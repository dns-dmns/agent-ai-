"""Resilient Gemini wrapper: retry/backoff, usage tracking, prompt trimming.

Every LLM call in the platform goes through :func:`generate_json` or
:func:`generate_text` so that transient 429/503 responses no longer kill a
generation round, and so token spend is recorded in ``logs/llm_usage.jsonl``.
"""

import asyncio
import json
import random
import re
import time
import warnings
from pathlib import Path
from typing import Any

from . import config

log = config.get_logger("llm")

# The old ``google.generativeai`` package is end-of-life. Prefer the maintained
# ``google-genai`` SDK when it is installed, and keep the legacy path working so
# nothing breaks on machines that have not upgraded yet.
NEW_SDK = False
try:  # pragma: no cover - depends on the local environment
    from google import genai as _new_genai
    from google.genai import types as _new_types

    NEW_SDK = True
except Exception:  # pragma: no cover
    _new_genai = None
    _new_types = None

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import google.generativeai as genai
    except Exception:  # pragma: no cover
        genai = None

if not NEW_SDK and genai is None:
    raise ImportError(
        "No Gemini SDK found. Install one of:\n"
        "  pip install google-genai            (recommended)\n"
        "  pip install google-generativeai     (legacy)"
    )

_USAGE_LOG: Path = config.LOG_DIR / "llm_usage.jsonl"
_configured = False

RETRYABLE_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "resource has been exhausted",
    "rate limit",
    "quota",
    "deadline exceeded",
    "temporarily unavailable",
    "internal error",
    "overloaded",
)

#: A *daily* quota cannot recover inside a 30-second backoff, so retrying it is
#: pure waste — and on a free tier of 20 requests a day, each retry spends one of
#: them. Three retries turned every exhausted call into four, which is how a
#: day's allowance disappeared in a handful of clicks.
DAILY_QUOTA_MARKERS = (
    "perdayperproject",
    "requestsperday",
    "per day",
    "daily limit",
    "generaterequestsperdayperprojectpermodel",
)
PER_MINUTE_QUOTA_MARKERS = (
    "perminuteperproject",
    "requestsperminute",
    "per minute",
)

_RETRY_DELAY_RE = re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)", re.IGNORECASE)


class QuotaKind(str):
    """How a quota failure should be handled: not at all, or after a wait."""


def classify_quota_error(err: object) -> str:
    """``"daily"``, ``"per_minute"`` or ``""`` for anything that is not a quota."""
    text = str(err).lower()
    if "429" not in text and "quota" not in text and "resource has been exhausted" not in text:
        return ""
    if any(marker in text for marker in DAILY_QUOTA_MARKERS):
        return "daily"
    if any(marker in text for marker in PER_MINUTE_QUOTA_MARKERS):
        return "per_minute"
    return "per_minute"          # unknown window: a short wait is the safe guess


def suggested_retry_delay(err: object, fallback: float) -> float:
    """The wait Google actually asked for, when it named one."""
    found = _RETRY_DELAY_RE.search(str(err))
    if not found:
        return fallback
    try:
        return min(float(found.group(1)) + 1.0, 65.0)
    except ValueError:
        return fallback


def quota_message(model_name: str, kind: str) -> str:
    """What a human should do about it — never "check your API key"."""
    used = calls_today(model_name)
    if kind == "daily":
        return "\n\n".join([
            f"The daily free-tier quota for {model_name} is exhausted "
            f"({used} request(s) logged today). This resets at midnight "
            f"US/Pacific — waiting a few seconds cannot help.",

            "Your options: enable billing on the Google Cloud project (a "
            "generation run costs a few cents), switch GEMINI_MODEL to a model "
            "with a larger free allowance, or use a key from a different "
            "project. Nothing is wrong with your API key.",

            "Running suites, verdicts, self-healing and reporting need no key "
            "at all — only generation does.",
        ])
    return (
        f"{model_name} is rate-limited for the moment (per-minute quota, "
        f"{used} request(s) logged today). Wait a minute and retry; if this "
        f"keeps happening, generate one module at a time."
    )


class LLMError(RuntimeError):
    """Raised when the model cannot be reached or returns unusable output."""


_client = None


def configure() -> None:
    global _configured, _client
    if _configured:
        return
    if not config.GEMINI_API_KEY:
        log.warning(
            "GEMINI_API_KEY is not set — AI generation endpoints will return a "
            "clear error until it is configured in .env"
        )
    elif NEW_SDK:
        _client = _new_genai.Client(api_key=config.GEMINI_API_KEY)
        log.info("Using the google-genai SDK (model %s).", config.GEMINI_MODEL)
    else:
        genai.configure(api_key=config.GEMINI_API_KEY)
        log.info(
            "Using the legacy google-generativeai SDK (model %s). "
            "Run 'pip install google-genai' to move onto the maintained SDK.",
            config.GEMINI_MODEL,
        )
    _configured = True


def require_key() -> None:
    if not config.GEMINI_API_KEY:
        raise LLMError(
            "GEMINI_API_KEY is missing. Copy .env.example to .env and set your "
            "Google AI Studio key, then restart the server."
        )


def trim_for_prompt(text: str, limit: int | None = None) -> str:
    """Clip oversized recorded-action payloads so prompts stay in budget."""
    limit = limit or config.PROMPT_ACTIONS_CHAR_LIMIT
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25) :]
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n... [{omitted} characters of recorded actions omitted for "
        f"prompt size; the steps above and below are representative] ...\n\n{tail}"
    )


def _record_usage(model_name: str, prompt: str, response_text: str, elapsed: float,
                  attempts: int, ok: bool) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model_name,
        "prompt_chars": len(prompt),
        "response_chars": len(response_text or ""),
        "approx_prompt_tokens": len(prompt) // 4,
        "approx_response_tokens": len(response_text or "") // 4,
        "elapsed_seconds": round(elapsed, 2),
        "attempts": attempts,
        "ok": ok,
    }
    try:
        with open(_USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _is_retryable(err: Exception) -> bool:
    msg = str(err).lower()
    return any(marker in msg for marker in RETRYABLE_MARKERS)


def _call_sync(prompt: str, *, system_instruction: str | None, model_name: str,
               json_mode: bool, images: list[dict] | None) -> str:
    """One blocking generate_content call, on whichever SDK is installed."""
    if NEW_SDK and _client is not None:
        parts: list[Any] = [prompt]
        for image in images or []:
            parts.append(_new_types.Part.from_bytes(
                data=image["data"], mime_type=image.get("mime_type", "image/png")
            ))
        cfg_kwargs: dict[str, Any] = {}
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        if json_mode:
            cfg_kwargs["response_mime_type"] = "application/json"
        response = _client.models.generate_content(
            model=model_name,
            contents=parts,
            config=_new_types.GenerateContentConfig(**cfg_kwargs) if cfg_kwargs else None,
        )
        return (response.text or "").strip()

    kwargs: dict[str, Any] = {"model_name": model_name}
    if system_instruction:
        kwargs["system_instruction"] = system_instruction
    model = genai.GenerativeModel(**kwargs)
    payload: Any = [prompt, *(images or [])] if images else prompt
    response = model.generate_content(
        payload,
        **({"generation_config": {"response_mime_type": "application/json"}}
           if json_mode else {}),
    )
    return (response.text or "").strip()


async def generate_text(
    prompt: str,
    *,
    system_instruction: str | None = None,
    model_name: str | None = None,
    json_mode: bool = True,
    max_retries: int | None = None,
    images: list[dict] | None = None,
) -> str:
    """Call Gemini off the event loop, retrying transient failures."""
    require_key()
    configure()
    retries = config.GEMINI_MAX_RETRIES if max_retries is None else max_retries
    resolved_model = model_name or config.GEMINI_MODEL
    started = time.time()
    last_error: Exception | None = None

    # Security: Redact PII, passwords, and tokens before sending to external LLM
    from . import mask
    prompt = mask.mask_text(prompt)
    if system_instruction:
        system_instruction = mask.mask_text(system_instruction)

    for attempt in range(1, retries + 2):
        try:
            text = await asyncio.to_thread(
                _call_sync, prompt,
                system_instruction=system_instruction, model_name=resolved_model,
                json_mode=json_mode, images=images,
            )
            _record_usage(resolved_model, prompt, text, time.time() - started, attempt, True)
            if not text:
                raise LLMError("Model returned an empty response.")
            return text
        except Exception as err:  # noqa: BLE001 - provider raises many types
            last_error = err
            quota = classify_quota_error(err)

            # A daily quota cannot recover inside a backoff window, and on a free
            # tier each attempt spends one of the day's requests. Retrying it
            # three times turned one exhausted call into four — which is how an
            # allowance of 20 vanished in a handful of clicks.
            if quota == "daily":
                _record_usage(resolved_model, prompt, "", time.time() - started,
                              attempt, False)
                log.warning("Daily quota exhausted for %s — not retrying.", resolved_model)
                raise LLMError(quota_message(resolved_model, "daily")) from err

            if attempt > retries or not _is_retryable(err):
                break

            backoff = min(2 ** attempt, 30) + random.uniform(0, 0.75)
            if quota == "per_minute":
                # Google says how long to wait; guessing shorter just fails again.
                backoff = suggested_retry_delay(err, backoff)
            log.warning(
                "Gemini call failed (attempt %s/%s): %s — retrying in %.1fs",
                attempt, retries + 1, err, backoff,
            )
            await asyncio.sleep(backoff)

    _record_usage(resolved_model, prompt, "", time.time() - started, retries + 1, False)
    if classify_quota_error(last_error) == "per_minute":
        raise LLMError(quota_message(resolved_model, "per_minute")) from last_error
    raise LLMError(f"Gemini request failed after {retries + 1} attempt(s): {last_error}")


def clean_json_text(raw_text: str) -> str:
    """Strip markdown fences and repair common LLM JSON shorthand."""
    import re

    raw_text = (raw_text or "").strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw_text = "\n".join(lines).strip()

    # "A".repeat(50) / "A" * 50  →  literal expanded string
    raw_text = re.sub(
        r'"([^"]*)"\.repeat\((\d+)\)',
        lambda m: '"' + (m.group(1) * min(int(m.group(2)), 5000)) + '"',
        raw_text,
    )
    raw_text = re.sub(
        r'"([^"]*)"\s*\*\s*(\d+)',
        lambda m: '"' + (m.group(1) * min(int(m.group(2)), 5000)) + '"',
        raw_text,
    )
    return raw_text


def _salvage_json(text: str) -> Any:
    """Last-ditch recovery: pull the largest balanced JSON block out of text."""
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    raise LLMError("Model response could not be parsed as JSON.")


async def generate_json(
    prompt: str,
    *,
    system_instruction: str | None = None,
    model_name: str | None = None,
    max_retries: int | None = None,
    images: list[dict] | None = None,
) -> Any:
    """Generate and parse a JSON payload, salvaging near-miss responses."""
    raw = await generate_text(
        prompt,
        system_instruction=system_instruction,
        model_name=model_name,
        json_mode=True,
        max_retries=max_retries,
        images=images,
    )
    cleaned = clean_json_text(raw)
    try:
        return json.loads(cleaned)
    except Exception as err:
        log.warning("Direct JSON parse failed (%s); attempting salvage.", err)
        return _salvage_json(cleaned)


def calls_today(model_name: str = "") -> int:
    """Requests logged today, optionally for one model.

    Every attempt is logged, including the failed ones, because Google counts
    them all against the quota. This is the number that matters when deciding
    whether a nine-call generation can even finish.
    """
    if not _USAGE_LOG.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    count = 0
    try:
        with open(_USAGE_LOG, "r", encoding="utf-8") as fh:
            for row in fh:
                if today not in row:
                    continue                    # cheap pre-filter
                try:
                    entry = json.loads(row)
                except Exception:
                    continue
                if not str(entry.get("ts", "")).startswith(today):
                    continue
                if model_name and entry.get("model") != model_name:
                    continue
                count += int(entry.get("attempts") or 1)
    except Exception:
        pass
    return count


async def list_models() -> list[str]:
    """Model ids this key can call ``generateContent`` on.

    Uses the REST metadata endpoint rather than a generation call, so it costs
    nothing against the quota — which makes it safe to run while exhausted, and
    it is the quickest way to tell an invalid key from a spent allowance.
    """
    import urllib.request

    require_key()
    url = ("https://generativelanguage.googleapis.com/v1beta/models"
           f"?key={config.GEMINI_API_KEY}&pageSize=200")

    def fetch() -> list[str]:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read())
        return sorted(
            entry["name"].replace("models/", "")
            for entry in payload.get("models", [])
            if "generateContent" in (entry.get("supportedGenerationMethods") or [])
        )

    return await asyncio.to_thread(fetch)


def budget_status(model_name: str = "", planned_calls: int = 1) -> dict:
    """Whether the day's allowance can plausibly cover a run of ``planned_calls``.

    The free tier is small enough that a multi-agent generation — three agents
    across up to three rounds — can exhaust it in one click. Saying so first is
    better than failing halfway through and leaving a partial suite behind.

    ``limit`` is a best guess, not something the API reports, so this warns and
    never blocks: an account with billing enabled has a far larger allowance and
    must not be second-guessed.
    """
    model = model_name or config.GEMINI_MODEL
    used = calls_today(model)
    limit = config.LLM_DAILY_BUDGET
    remaining = max(0, limit - used) if limit else None
    return {
        "model": model,
        "calls_today": used,
        "assumed_daily_limit": limit,
        "remaining": remaining,
        "planned_calls": planned_calls,
        "enough": True if not limit else remaining >= planned_calls,
        "exhausted": bool(limit) and remaining == 0,
    }


def usage_summary(limit: int = 500) -> dict:
    """Aggregate the tail of the usage log for the dashboard."""
    if not _USAGE_LOG.exists():
        return {"calls": 0, "approx_prompt_tokens": 0, "approx_response_tokens": 0, "failures": 0}
    calls = prompt_tokens = response_tokens = failures = 0
    try:
        with open(_USAGE_LOG, "r", encoding="utf-8") as fh:
            rows = fh.readlines()[-limit:]
        for row in rows:
            try:
                entry = json.loads(row)
            except Exception:
                continue
            calls += 1
            prompt_tokens += entry.get("approx_prompt_tokens", 0)
            response_tokens += entry.get("approx_response_tokens", 0)
            if not entry.get("ok"):
                failures += 1
    except Exception:
        pass
    return {
        "calls": calls,
        "approx_prompt_tokens": prompt_tokens,
        "approx_response_tokens": response_tokens,
        "failures": failures,
    }
