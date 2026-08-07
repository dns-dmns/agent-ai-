"""Verdict engine — decides whether a scenario actually met its expectation.

Before this module existed a scenario was marked PASS whenever no Playwright
step threw an exception. That meant every negative / boundary / security case
"passed" simply by typing bad data and clicking Save, with nothing checking
that the validation error the test expected ever appeared.

The engine works in two layers:

* **rules**  – deterministic. Classifies the intent of ``expected_result`` and
  matches it against evidence collected from the live page (inline validation
  messages, toast text, native dialogs, row presence, URL changes, explicit
  VERIFY_* assertions).
* **ai**     – optional. For cases the rules cannot decide, a single Gemini
  call inspects the final screenshot plus the collected evidence and returns a
  verdict. Falls back to UNVERIFIED when no API key is configured.

Statuses: ``PASS``, ``FAIL``, ``UNVERIFIED`` (executed cleanly but nothing
proved the expectation), ``CRASH`` (browser died — set by the executor).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIED = "UNVERIFIED"

# ──────────────────────────────────────────────────────────────────────
# Message classification
# ──────────────────────────────────────────────────────────────────────
ERROR_SELECTOR_HINTS = (
    "p-error", "invalid-feedback", "field-error", "error-message", "alert-danger",
    "toast-message-error", "text-danger", "is-invalid", "ng-invalid", "error",
)
SUCCESS_SELECTOR_HINTS = (
    "toast-message-success", "alert-success", "toast-success", "text-success",
    "p-message-success",
)

ERROR_TEXT_HINTS = (
    "required", "invalid", "must", "cannot", "can not", "can't", "not allowed",
    "already exists", "duplicate", "error", "failed", "fail", "not valid",
    "should be", "minimum", "maximum", "exceed", "too long", "too short",
    "please enter", "please select", "mandatory", "no records", "not found",
    "unauthorized", "denied", "incorrect", "mismatch", "unable",
)
SUCCESS_TEXT_HINTS = (
    "success", "successfully", "saved", "created", "updated", "deleted",
    "added", "completed", "submitted",
    # Wordings real applications use for the same thing. Without them the message
    # was classified "unknown", so a scenario that had visible confirmation on
    # screen still came back UNVERIFIED.
    "accepted", "approved", "activated", "deactivated", "registered",
    "uploaded", "downloaded", "exported", "imported", "sent",
)


def classify_message(text: str, selector: str = "") -> str:
    """Return ``error`` / ``success`` / ``unknown`` for a detected UI message."""
    sel = (selector or "").lower()
    body = (text or "").lower().strip()

    for hint in SUCCESS_SELECTOR_HINTS:
        if hint in sel:
            return "success"
    for hint in ERROR_SELECTOR_HINTS:
        if hint in sel:
            return "error"

    has_success = any(hint in body for hint in SUCCESS_TEXT_HINTS)
    has_error = any(hint in body for hint in ERROR_TEXT_HINTS)
    if has_error and not has_success:
        return "error"
    if has_success and not has_error:
        return "success"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────
# Expectation classification
# ──────────────────────────────────────────────────────────────────────
EXPECT_ERROR_PATTERNS = (
    r"\bvalidation\b", r"\berror\b", r"\brequired\b", r"\bmandatory\b",
    r"\binvalid\b", r"\bduplicate\b", r"\balready exists\b",
    r"\breject(s|ed|ing)?\b",
    r"\bprevent(s|ed|ing)?\b", r"\bblock(s|ed|ing)?\b", r"\bnot allowed\b",
    r"\bshould not be (saved|created|accepted|allowed)\b",
    r"\bmust not\b", r"\bcannot be\b", r"\bis not saved\b", r"\bfails?\b",
    r"\bwarning\b", r"\bdenied\b", r"\btruncat(e|ed|es)\b", r"\brestrict",
    r"\bdisallow", r"\brefus(e|ed|es)\b",
)
EXPECT_SUCCESS_PATTERNS = (
    r"\bsuccessfully\b", r"\bsuccess\b", r"\bis (created|saved|updated|added)\b",
    r"\bare (created|saved|updated|added)\b", r"\bappears? in\b",
    r"\bis (displayed|shown|visible)\b", r"\bredirect", r"\bnavigat(e|es|ed)\b",
    r"\bis listed\b", r"\bshows?\b", r"\bopens?\b", r"\bcloses?\b",
    r"\bfilters?\b", r"\bsorts?\b", r"\bdownload",
    # Phrasings the real suites use constantly, which used to fall through as
    # "unclassified" and therefore always came back UNVERIFIED.
    r"\baccept(s|ed)\b", r"\bpersist(s|ed)\b", r"\bupdate(s|d)\b",
    r"\brender(s|ed)\b", r"\brestore(s|d)\b", r"\bre-?order(s|ed)?\b",
    r"\bmask(s|ed)\b", r"\btrims?\b", r"\bclear(s|ed)\b", r"\bfocus",
    r"\ballows?\b", r"\benabled\b", r"\baligned\b", r"\bsubmits?\b",
    r"\bselected\b", r"\bready for\b", r"\bdisplays?\b", r"\bpopulat(e|es|ed)\b",
    r"\bcalculated\b", r"\bdisabled\b", r"\bread-?only\b", r"\bno matching rows\b",
)
# Expectations that only assert the app did not fall over. Verifiable from the
# absence of console errors plus a page that still responds.
EXPECT_STABILITY_PATTERNS = (
    r"without crashing", r"does not crash", r"\bno unhandled\b", r"gracefully",
    r"without breaking", r"without exposing", r"without internal error",
    r"do(es)? not corrupt", r"without throwing", r"no errors? (occur|are thrown)",
    r"handled? safely", r"handles? .* safely", r"safely",
)
EXPECT_ABSENCE_PATTERNS = (
    r"\bremoved\b", r"\bdeleted\b", r"\bno longer (appears|exists|listed|visible)\b",
    r"\bexcluded\b", r"\bnot (listed|present|shown|available|visible)\b",
    r"\bhidden from\b", r"\bdisappears?\b", r"\bcount = 0\b", r"\bempty\b",
)
EXPECT_PERSIST_PATTERNS = (
    r"\bremains?\b", r"\bstill (present|exists|listed|visible|there)\b",
    r"\bunchanged\b", r"\bis not (deleted|removed|modified)\b",
    r"\bpersists?\b", r"\bretained\b",
)
EXPECT_SANITISED_PATTERNS = (
    r"\bsanitiz", r"\bsanitis", r"\bescap(e|ed|ing)\b", r"\bno script\b",
    r"\bnot execute", r"\bas plain text\b", r"\bliteral", r"\bno alert\b",
    r"\bdoes not crash\b", r"\bno sql\b", r"\bwithout crashing\b",
)


def classify_expectation(expected_result: str) -> dict:
    """Extract the verifiable intent from a natural-language expectation."""
    text = (expected_result or "").lower()

    def any_match(patterns) -> bool:
        return any(re.search(p, text) for p in patterns)

    intent = {
        "expects_error": any_match(EXPECT_ERROR_PATTERNS),
        "expects_success": any_match(EXPECT_SUCCESS_PATTERNS),
        "expects_absence": any_match(EXPECT_ABSENCE_PATTERNS),
        "expects_persistence": any_match(EXPECT_PERSIST_PATTERNS),
        "expects_sanitised": any_match(EXPECT_SANITISED_PATTERNS),
        "expects_stability": any_match(EXPECT_STABILITY_PATTERNS),
        "has_text": bool(text.strip()),
    }
    # An expectation phrased as "error appears ... record is not created" is
    # fundamentally a negative check; the error signal wins.
    if intent["expects_error"]:
        intent["expects_success"] = False
    return intent


def extract_quoted_values(expected_result: str) -> list[str]:
    """Pull quoted literals out of an expectation, e.g. message text to match."""
    return [
        m.strip()
        for m in re.findall(r"[\"'`]([^\"'`]{3,80})[\"'`]", expected_result or "")
        if m.strip()
    ]


# ──────────────────────────────────────────────────────────────────────
# Evidence
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Evidence:
    """Everything observed while a scenario ran, used to judge the outcome."""

    messages: list[dict] = field(default_factory=list)   # {text, selector, kind}
    dialogs: list[dict] = field(default_factory=list)    # {type, message, action}
    console_errors: list[str] = field(default_factory=list)
    network_errors: list[str] = field(default_factory=list)
    assertions: list[dict] = field(default_factory=list)  # {action, selector, ok, detail}
    downloads: list[dict] = field(default_factory=list)
    url_start: str = ""
    url_end: str = ""
    steps_total: int = 0
    steps_executed: int = 0
    healed_steps: int = 0
    notes: list[str] = field(default_factory=list)      # captures, polls, API calls
    auto_filled: list[dict] = field(default_factory=list)   # fields the engine completed itself
    submission_blocked: dict = field(default_factory=dict)  # the app refused an invalid submit
    input_rejected: list[dict] = field(default_factory=list)  # widget refused the keystrokes
    wording_mismatches: list[dict] = field(default_factory=list)  # step literal != real text
    rule_results: list[dict] = field(default_factory=list)  # {id, title, ok, detail}

    def add_message(self, text: str, selector: str = "") -> None:
        text = (text or "").strip()
        if not text or len(text) > 400:
            return
        if any(m["text"] == text for m in self.messages):
            return
        self.messages.append(
            {"text": text, "selector": selector, "kind": classify_message(text, selector)}
        )

    @property
    def error_messages(self) -> list[str]:
        return [m["text"] for m in self.messages if m["kind"] == "error"]

    @property
    def success_messages(self) -> list[str]:
        return [m["text"] for m in self.messages if m["kind"] == "success"]

    @property
    def unknown_messages(self) -> list[str]:
        return [m["text"] for m in self.messages if m["kind"] == "unknown"]

    @property
    def severe_console_errors(self) -> list[str]:
        """Console entries logged at error level (warnings are routine noise)."""
        return [e for e in self.console_errors if e.lower().startswith("[error]")]

    @property
    def explicit_assertions(self) -> list[dict]:
        return list(self.assertions)

    def to_dict(self) -> dict:
        return {
            "messages": self.messages,
            "dialogs": self.dialogs,
            "assertions": self.assertions,
            "downloads": self.downloads,
            "url_start": self.url_start,
            "url_end": self.url_end,
            "steps_total": self.steps_total,
            "steps_executed": self.steps_executed,
            "healed_steps": self.healed_steps,
            "console_error_count": len(self.console_errors),
            "network_error_count": len(self.network_errors),
            "notes": self.notes,
            "rule_results": self.rule_results,
            "auto_filled": self.auto_filled,
            "submission_blocked": self.submission_blocked,
            "input_rejected": self.input_rejected,
            "wording_mismatches": self.wording_mismatches,
        }


@dataclass
class Verdict:
    status: str
    reason: str
    source: str = "rules"          # rules | ai | assertions | steps
    confidence: float = 1.0
    evidence_summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "source": self.source,
            "confidence": self.confidence,
            "evidence": self.evidence_summary,
        }


# ──────────────────────────────────────────────────────────────────────
# Rule-based evaluation
# ──────────────────────────────────────────────────────────────────────
def evaluate(expected_result: str, evidence: Evidence, scenario_type: str = "",
             message_surface_known: bool = True) -> Verdict:
    """Judge a scenario that completed all of its steps without throwing."""
    intent = classify_expectation(expected_result)
    summary = evidence.to_dict()
    stype = (scenario_type or "").lower()

    failed_assertions = [a for a in evidence.assertions if not a.get("ok")]
    passed_assertions = [a for a in evidence.assertions if a.get("ok")]

    # 1. An explicit assertion that failed is decisive (the executor normally
    #    raises, but soft assertions land here).
    if failed_assertions:
        detail = failed_assertions[0].get("detail") or failed_assertions[0].get("action")
        return Verdict(FAIL, f"Assertion failed: {detail}", "assertions", 1.0, summary)

    # 2. A JavaScript dialog carrying an injected payload means the app executed
    #    the payload — a genuine security defect, regardless of the expectation.
    for dialog in evidence.dialogs:
        msg = (dialog.get("message") or "").lower()
        if any(token in msg for token in ("xss", "alert(", "1=1", "<script")):
            return Verdict(
                FAIL,
                f"Injected payload executed in the browser (dialog said: "
                f"{dialog.get('message')!r}) — the input is not sanitised.",
                "rules", 1.0, summary,
            )

    quoted = extract_quoted_values(expected_result)

    # 1b. The step demanded wording the application does not use.
    #     The application responded — it just said something else. Failing here
    #     blames a working app for a literal the generator guessed; passing would
    #     claim the specific behaviour was confirmed when it was not. Neither is
    #     true, so this is exactly what UNVERIFIED is for, and the real wording is
    #     named so the step can be corrected.
    mismatches = getattr(evidence, "wording_mismatches", None)
    if mismatches:
        first = mismatches[0]
        actual = ", ".join(repr(a) for a in (first.get("actual") or [])[:2])
        return Verdict(
            UNVERIFIED,
            f"The application responded, but not with the wording this step "
            f"required: it expected {first.get('expected')!r} and the screen showed "
            f"{actual or 'something else'}. The application may well be correct — "
            f"update the step to the wording above, then re-run to confirm.",
            "rules", 0.5, summary,
        )

    # 2b. The application refused to submit at all.
    #     Many forms keep Save disabled until every mandatory field is valid. That
    #     refusal is the validation — it is a stronger guarantee than an error
    #     message, because the invalid data never reached the server. A scenario
    #     that expected rejection has been satisfied; one that expected a save has
    #     genuinely failed.
    blocked = getattr(evidence, "submission_blocked", None)
    if blocked:
        empty = ", ".join(str(x) for x in (blocked.get("empty_required") or []))
        if intent["expects_error"] or intent["expects_absence"]:
            return Verdict(
                PASS,
                f"The application blocked the submission: '{blocked.get('control')}' stayed "
                f"disabled because {empty or 'a required field'} was empty, so the invalid "
                f"record was never created.",
                "rules", 0.9, summary,
            )
        if intent["expects_success"]:
            missing = empty or "a required field"
            return Verdict(
                FAIL,
                f"A successful save was expected, but the application would not accept the "
                f"form: '{blocked.get('control')}' is disabled while {missing} is empty. "
                f"The scenario is missing a value this form demands.",
                "rules", 0.9, summary,
            )

    # 2c. The field itself refused the keystrokes.
    #     A numeric input that drops letters, or a maxlength that truncates, is
    #     the application enforcing its rule at the input layer. Nothing invalid
    #     was ever submitted, so there is no error message to find — reporting
    #     "the invalid input appears to have been accepted" would be exactly
    #     backwards.
    refused = getattr(evidence, "input_rejected", None)
    if refused and (intent["expects_error"] or intent["expects_absence"]) \
            and not evidence.error_messages:
        first = refused[0]
        return Verdict(
            PASS,
            f"The field rejected the input before it could be submitted: "
            f"{first.get('detail')}. The application enforces this at the input "
            f"layer rather than with a message, so nothing invalid was stored.",
            "rules", 0.85, summary,
        )

    # 3. Negative / validation expectations.
    if intent["expects_error"]:
        if evidence.error_messages:
            matched = evidence.error_messages
            if quoted:
                lowered = [m.lower() for m in matched]
                hit = [q for q in quoted if any(q.lower() in m for m in lowered)]
                if hit:
                    return Verdict(
                        PASS,
                        f"Expected validation surfaced: {matched[0]!r} (matched {hit[0]!r}).",
                        "rules", 1.0, summary,
                    )
            return Verdict(
                PASS, f"Expected validation surfaced: {matched[0]!r}.", "rules", 0.9, summary
            )
        if any(d.get("type") in ("alert", "confirm") for d in evidence.dialogs):
            return Verdict(
                PASS,
                f"Application blocked the action with a dialog: "
                f"{evidence.dialogs[0].get('message')!r}.",
                "rules", 0.8, summary,
            )
        if intent["expects_sanitised"] and not evidence.dialogs:
            return Verdict(
                UNVERIFIED,
                "Payload was submitted and nothing executed, but no explicit check "
                "confirmed the stored value is escaped. Add a VERIFY step to assert "
                "the rendered text.",
                "rules", 0.5, summary,
            )
        # Never convert ignorance into a FAIL. If this application's message
        # surface has never been observed, "nothing appeared" and "something
        # appeared that I cannot read" are indistinguishable, and blaming a
        # working application is the most damaging thing this tool can do.
        from . import config as _config       # lazy, as elsewhere in this module
        if _config.STRICT_MESSAGE_CONFIDENCE and not message_surface_known:
            return Verdict(
                UNVERIFIED,
                "No validation was detected, but this application's message surface "
                "has never been observed — a message may have appeared that the "
                "scanner cannot read. Run the flow once with a deliberate failure so "
                "the engine can learn where messages render, then re-run.",
                "rules", 0.4, summary,
            )
        return Verdict(
            FAIL,
            "Expected a validation / error response, but the application showed "
            "none — the invalid input appears to have been accepted.",
            "rules", 0.85, summary,
        )

    # 4. Positive expectations.
    if intent["expects_success"] or intent["expects_absence"] or intent["expects_persistence"]:
        if evidence.error_messages:
            return Verdict(
                FAIL,
                f"Unexpected validation error during a positive scenario: "
                f"{evidence.error_messages[0]!r}.",
                "rules", 0.9, summary,
            )
        if passed_assertions:
            actions = ", ".join(sorted({a.get("action", "VERIFY") for a in passed_assertions}))
            return Verdict(
                PASS, f"Expectation confirmed by explicit assertions ({actions}).",
                "assertions", 1.0, summary,
            )
        if evidence.success_messages:
            return Verdict(
                PASS, f"Success confirmation shown: {evidence.success_messages[0]!r}.",
                "rules", 0.9, summary,
            )
        if intent["expects_success"] and re.search(
            r"\b(redirect|navigat)", (expected_result or "").lower()
        ) and evidence.url_end and evidence.url_end != evidence.url_start:
            return Verdict(
                PASS,
                f"Navigation occurred: {evidence.url_start} → {evidence.url_end}.",
                "rules", 0.75, summary,
            )
        if evidence.downloads and re.search(
            r"\bdownload|export|excel|pdf|csv", (expected_result or "").lower()
        ):
            return Verdict(
                PASS, f"File downloaded: {evidence.downloads[0].get('suggested_filename')}.",
                "rules", 0.9, summary,
            )
        # "System safely accepts …" is both a success and a stability claim. When
        # nothing confirms the success half, the stability half is still checkable,
        # so fall through instead of giving up.
        if not (intent["expects_stability"] or intent["expects_sanitised"]):
            return Verdict(
                UNVERIFIED,
                "All steps executed, but nothing on the page confirmed the expected "
                "outcome (no success message, no assertion). Add a VERIFY_ROW_CONTAINS "
                "or VERIFY_VALUE step to make this scenario provable.",
                "rules", 0.5, summary,
            )

    # 5. "Handled safely / without crashing / escaped as literal text".
    #    Verifiable from the absence of a payload dialog and of severe console
    #    errors — which is exactly what these scenarios are really asserting.
    if intent["expects_stability"] or intent["expects_sanitised"]:
        severe = evidence.severe_console_errors
        if severe:
            return Verdict(
                FAIL,
                f"The application logged {len(severe)} console error(s) while handling this "
                f"input — it did not cope gracefully: {severe[0][:160]}",
                "rules", 0.8, summary,
            )
        if passed_assertions:
            return Verdict(
                PASS,
                "Input was handled without executing a payload or logging errors, and the "
                "explicit assertions passed.",
                "assertions", 0.9, summary,
            )
        if evidence.error_messages:
            return Verdict(
                PASS,
                f"Input was rejected cleanly with a validation message "
                f"({evidence.error_messages[0]!r}) and nothing executed.",
                "rules", 0.8, summary,
            )
        return Verdict(
            PASS,
            "No payload executed, no dialog fired and no console errors were logged — "
            "the input was handled without breaking the page.",
            "rules", 0.7, summary,
        )

    # 6. No parseable expectation at all.
    if passed_assertions:
        return Verdict(PASS, "Explicit assertions passed.", "assertions", 1.0, summary)
    if evidence.error_messages and stype.startswith("positive"):
        return Verdict(
            FAIL,
            f"Validation error appeared during a positive scenario: "
            f"{evidence.error_messages[0]!r}.",
            "rules", 0.8, summary,
        )
    return Verdict(
        UNVERIFIED,
        "Steps executed cleanly, but the expectation could not be interpreted "
        "into a checkable condition. Rephrase the expected result (name the "
        "message, record or state to look for) or add a VERIFY step.",
        "rules", 0.4, summary,
    )


# ──────────────────────────────────────────────────────────────────────
# AI adjudication (optional second layer)
# ──────────────────────────────────────────────────────────────────────
def build_ai_prompt(scenario_name: str, expected_result: str, steps: list[str],
                    evidence: Evidence) -> str:
    return f"""
You are a senior QA analyst adjudicating one automated test scenario.
Decide, strictly from the evidence, whether the expected result was achieved.

SCENARIO: {scenario_name}
EXPECTED RESULT: {expected_result}

STEPS EXECUTED:
{json.dumps(steps[:60], indent=2)}

OBSERVED EVIDENCE:
- Inline / toast messages detected: {json.dumps(evidence.messages, indent=2)}
- Native browser dialogs: {json.dumps(evidence.dialogs, indent=2)}
- Explicit assertion results: {json.dumps(evidence.assertions, indent=2)}
- Downloads captured: {json.dumps(evidence.downloads, indent=2)}
- URL at start: {evidence.url_start}
- URL at end: {evidence.url_end}
- Console errors: {len(evidence.console_errors)} | Network errors: {len(evidence.network_errors)}

A screenshot of the final page state is attached.

RULES:
- If the expectation was a validation/error and no error is visible in the
  evidence or screenshot, the verdict is FAIL.
- If the expectation was a successful save and the screenshot shows an error
  or the record is absent, the verdict is FAIL.
- Answer UNVERIFIED only when the evidence genuinely cannot settle it.
- Judge only what the evidence shows. Never assume success.

Return ONLY this JSON:
{{"status": "PASS" | "FAIL" | "UNVERIFIED",
  "reason": "one sentence citing the specific evidence",
  "confidence": 0.0-1.0}}
""".strip()


async def ai_adjudicate(scenario_name: str, expected_result: str, steps: list[str],
                        evidence: Evidence, screenshot_bytes: bytes | None) -> Verdict | None:
    """Ask Gemini to settle a scenario. Returns None if unavailable."""
    from . import config
    from . import llm

    if not config.GEMINI_API_KEY:
        return None
    try:
        images = (
            [{"mime_type": "image/png", "data": screenshot_bytes}] if screenshot_bytes else None
        )
        data = await llm.generate_json(
            build_ai_prompt(scenario_name, expected_result, steps, evidence),
            system_instruction=(
                "You are a precise QA adjudicator. Output only valid JSON. "
                "Never claim success without supporting evidence."
            ),
            images=images,
        )
        status = str(data.get("status", "")).upper()
        if status not in (PASS, FAIL, UNVERIFIED):
            return None
        return Verdict(
            status,
            str(data.get("reason", "AI adjudication."))[:500],
            "ai",
            float(data.get("confidence", 0.7)),
            evidence.to_dict(),
        )
    except Exception as err:  # noqa: BLE001
        from . import config as _c

        _c.get_logger("verdict").warning("AI adjudication failed: %s", err)
        return None
