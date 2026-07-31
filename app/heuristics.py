"""Execution heuristics: idle waiting, variable substitution, self-healing.

Pillars 1, 2, 4 and 5 of the scaling roadmap live here. Vision healing now has
safety rails: it reports a confidence score, can be disabled globally, and is
never allowed to click coordinates for destructive actions (Delete / Submit /
Confirm), because a mis-located click there would mutate real data.
"""

from __future__ import annotations

import re

from . import config

log = config.get_logger("heuristics")

# Word-aware matching on purpose. A plain substring test (the original behaviour)
# treated the letter "x" as "close", so a failing click on #taxCode or #exportBtn
# fell through to the Cancel button and silently dismissed the form — the test
# then "passed" against the wrong element.
DISMISS_WORDS = ("cancel", "close", "dismiss", "abort", "discard", "reject")
DISMISS_LITERALS = (
    "p-dialog-header-close", "btn-close", "pi-times", "close-icon", "modal-close",
)
_ARIA_CLOSE = re.compile(r"aria-label\s*=\s*[\"']?\s*close", re.IGNORECASE)


def looks_like_dismiss(selector: str) -> bool:
    """True when a selector plausibly targets a Cancel/Close/dismiss control.

    Matches ``#cancelBtn``, ``.btn-close``, ``i.pi-times`` and
    ``button[aria-label="Close"]`` — but not ``#taxCode``, ``#exportBtn`` or
    ``#closeness``, where the keyword is only part of a longer word.
    """
    if not selector:
        return False
    lowered = selector.lower()
    if any(literal in lowered for literal in DISMISS_LITERALS):
        return True
    if _ARIA_CLOSE.search(selector):
        return True
    for word in DISMISS_WORDS:
        for match in re.finditer(word, lowered):
            start, end = match.span()
            before = selector[start - 1] if start > 0 else ""
            after = selector[end] if end < len(selector) else ""
            if before.isalpha():          # part of a longer word ("unclosed")
                continue
            if after.islower():           # continues the word ("closeness")
                continue
            return True
    return False

SPINNER_SELECTORS = (
    ".p-component-overlay, .spinner, .loading-bar, .p-progressbar, .p-skeleton, "
    ".mat-progress-spinner, .ant-spin, .loading-overlay, [aria-busy='true']"
)


async def wait_for_ui_idle(page, max_wait_ms: int = 3000) -> None:
    """PILLAR 4: pause until the DOM settles and loading overlays detach."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=max_wait_ms)
    except Exception:
        pass

    try:
        spinners = page.locator(SPINNER_SELECTORS)
        if await spinners.count() > 0:
            try:
                await spinners.first.wait_for(state="detached", timeout=max_wait_ms)
            except Exception:
                try:
                    await spinners.first.wait_for(state="hidden", timeout=max_wait_ms // 2)
                except Exception:
                    pass
    except Exception:
        pass


def resolve_dynamic_variables(text_str, runtime_pool: dict):
    """PILLAR 1: replace ``{{token}}`` placeholders with runtime pool values."""
    if not isinstance(text_str, str) or not text_str or "{{" not in text_str:
        return text_str
    result = text_str
    for key, val in (runtime_pool or {}).items():
        if val is None:
            continue
        placeholder = "{{" + str(key) + "}}"
        if placeholder in result:
            result = result.replace(placeholder, str(val))
    return result


def _is_destructive(text: str) -> bool:
    low = (text or "").lower()
    return any(word in low for word in config.DESTRUCTIVE_KEYWORDS)


async def vision_ai_locate_element(page, selector: str) -> tuple[int, int, float] | None:
    """PILLAR 2: ask Gemini Vision where an element is when CSS lookup fails.

    Returns ``(x_px, y_px, confidence)`` or ``None``.
    """
    if not config.VISION_HEALING_ENABLED:
        return None
    if not config.GEMINI_API_KEY:
        return None

    from . import llm

    try:
        screenshot_bytes = await page.screenshot(type="png")
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        prompt = f"""
        Analyse this webpage screenshot ({viewport['width']}x{viewport['height']} px).
        Locate the UI element matching this selector/description: '{selector}'
        Return ONLY JSON with the element centre as viewport percentages and your
        confidence that this is the correct element:
        {{"x_pct": 45.5, "y_pct": 62.0, "confidence": 0.85, "label": "what you found"}}
        If it is not visible, return {{"x_pct": 0, "y_pct": 0, "confidence": 0}}.
        """.strip()

        data = await llm.generate_json(
            prompt,
            model_name=config.GEMINI_VISION_MODEL,
            images=[{"mime_type": "image/png", "data": screenshot_bytes}],
            max_retries=1,
        )
        if isinstance(data, list):
            data = data[0] if data else {}
        x_pct = float(data.get("x_pct", 0) or 0)
        y_pct = float(data.get("y_pct", 0) or 0)
        confidence = float(data.get("confidence", 0) or 0)

        if x_pct <= 0 or y_pct <= 0:
            return None
        if confidence and confidence < config.VISION_MIN_CONFIDENCE:
            log.info(
                "Vision healing declined for %r — confidence %.2f below threshold %.2f",
                selector, confidence, config.VISION_MIN_CONFIDENCE,
            )
            return None

        x_px = int((x_pct / 100.0) * viewport["width"])
        y_px = int((y_pct / 100.0) * viewport["height"])
        log.info(
            "Vision healing located %r at (%s, %s) confidence %.2f (label=%s)",
            selector, x_px, y_px, confidence or 1.0, data.get("label", ""),
        )
        return (x_px, y_px, confidence or 1.0)
    except Exception as err:  # noqa: BLE001
        log.info("Vision AI locator fallback notice: %s", err)
        return None


async def attempt_auto_healing(page, action: str, selector: str, value: str = "",
                               allow_vision: bool = True) -> bool:
    """PILLAR 5 + 2: clear blockers, retry candidate locators, then Vision AI."""
    # 1. Clear anything overlaying the page (toast, modal backdrop).
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)
    except Exception:
        pass
    try:
        await page.evaluate(
            """() => {
                const kill = document.querySelectorAll(
                    '.p-toast-message, .toast, .snackbar, .p-component-overlay-leave');
                kill.forEach(el => el.remove());
                const backdrop = document.querySelector('.p-dialog-mask, .modal-backdrop');
                if (backdrop) backdrop.click();
            }"""
        )
    except Exception:
        pass

    clean_id = selector.lstrip("#").strip()

    async def try_candidates(candidates: list[str]) -> bool:
        for cand in candidates:
            if not cand:
                continue
            try:
                loc = page.locator(cand).first
                if await loc.count() > 0 and await loc.is_visible():
                    if action == "TYPE":
                        await loc.fill(value, timeout=3000)
                    else:
                        await loc.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    if action == "TYPE":
        healed = await try_candidates([
            selector,
            f"input[name='{clean_id}']",
            f"input[formcontrolname='{clean_id}']",
            f"input[placeholder*='{clean_id}']",
            f"textarea[name='{clean_id}']",
            f"[aria-label='{clean_id}']",
        ])
    else:
        candidates = [
            selector,
            f"button:has-text('{clean_id}')",
            f"a:has-text('{clean_id}')",
            f"[aria-label='{clean_id}']",
        ]
        # Only offer dialog-close controls when the step was actually trying to
        # dismiss something — otherwise a failed click on an unrelated field
        # would close the dialog and the test would "pass" on the wrong element.
        if looks_like_dismiss(selector):
            candidates += [".p-dialog-header-close", ".btn-close", "button[aria-label='Close']"]
        healed = await try_candidates(candidates)
    if healed:
        return True

    # 2. Vision AI coordinate click — never for destructive controls.
    if not allow_vision:
        log.info("Vision healing skipped for destructive target %r", selector)
        return False
    if _is_destructive(selector) or _is_destructive(value):
        log.info("Vision healing blocked: %r looks destructive", selector)
        return False

    located = await vision_ai_locate_element(page, selector)
    if not located:
        return False

    x_px, y_px, _confidence = located
    try:
        await page.mouse.click(x_px, y_px)
        if action == "TYPE":
            await page.keyboard.type(value)
        return True
    except Exception:
        return False
