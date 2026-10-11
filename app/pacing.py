"""Test speed: wait while the application is busy, never after it is ready.

Runs were slow because every wait was a fixed sleep: a 0.5-0.8 s pause after
every step, 0.3 s of ``slow_mo`` on every headed action, ``WAIT`` steps that
always slept their full time, and a spinner check that counted *hidden*
spinners and so waited out its whole timeout on applications that keep one in
the DOM. A 30-step headed scenario spent about half a minute doing nothing.

``settle()`` replaces those sleeps. It returns as soon as the page is quiet --
no XHR/fetch in flight, no *visible* loading indicator, no DOM change for a
short window -- and only waits the full limit when something is genuinely
still loading. The chosen speed sets the limits:

    fast     shortest limits, no extra pauses
    normal   smart waits with room for slower screens (default)
    careful  the old fixed pauses, for slow or unstable environments
"""
from __future__ import annotations

import time
import weakref
from dataclasses import dataclass


@dataclass(frozen=True)
class Pace:
    name: str
    step_pause_ms: int        # fixed pause after each step (careful only)
    settle_max_ms: int        # longest a smart wait may take after a step
    quiet_ms: int             # how long the page must stay still to count as ready
    wait_steps_fixed: bool    # WAIT steps sleep their full time (careful) or "up to"
    slow_mo_headed: int       # Playwright slow_mo when watching the browser
    retry_backoff_ms: int     # pause before retrying a failed click/type


SPEEDS: dict[str, Pace] = {
    "fast": Pace("fast", 0, 3000, 150, False, 0, 400),
    "normal": Pace("normal", 0, 8000, 300, False, 80, 1000),
    "careful": Pace("careful", 500, 10000, 400, True, 300, 2000),
}
DEFAULT_SPEED = "normal"


def pace_for(speed: str) -> Pace:
    return SPEEDS.get((speed or "").strip().lower(), SPEEDS[DEFAULT_SPEED])


# Visible-only: an application that keeps a hidden spinner in the DOM must not
# make every wait run to its limit.
BUSY_JS = """() => {
  const SEL = '.p-component-overlay:not(.p-dialog-mask), .spinner, .loading-bar, .p-progressbar, '
            + '.p-progress-spinner, .p-skeleton, .mat-progress-spinner, .ant-spin, '
            + '.loading-overlay, [aria-busy="true"]';
  const visible = el => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
           && parseFloat(s.opacity || '1') > 0.05;
  };
  const spinner = [...document.querySelectorAll(SEL)].some(visible);
  return {
    ready: document.readyState !== 'loading',
    spinner,
    lastMutation: window.__qaLastMutation || 0,
    now: performance.now(),
  };
}"""

# Installed with add_init_script so it survives navigation.
MUTATION_CLOCK_JS = """(() => {
  if (window.__qaMutationClock) return;
  window.__qaMutationClock = true;
  window.__qaLastMutation = performance.now();
  const start = () => {
    try {
      new MutationObserver(() => { window.__qaLastMutation = performance.now(); })
        .observe(document.documentElement, {childList: true, subtree: true, attributes: true,
                                            characterData: true});
    } catch (e) {}
  };
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
})();"""

# A request older than this is treated as long-polling / streaming, not loading.
_STALE_REQUEST_S = 6.0
_inflight: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


async def track(page) -> None:
    """Start counting this page's in-flight XHR/fetch requests."""
    pending: dict = {}
    _inflight[page] = pending

    def started(req):
        if req.resource_type in ("xhr", "fetch", "document"):
            pending[req] = time.monotonic()

    def finished(req):
        pending.pop(req, None)

    page.on("request", started)
    page.on("requestfinished", finished)
    page.on("requestfailed", finished)
    try:
        await page.add_init_script(MUTATION_CLOCK_JS)
        await page.evaluate(MUTATION_CLOCK_JS)
    except Exception:
        pass


def _requests_busy(page) -> bool:
    pending = _inflight.get(page)
    if not pending:
        return False
    now = time.monotonic()
    return any(now - t < _STALE_REQUEST_S for t in pending.values())


async def settle(page, max_ms: int, quiet_ms: int = 300) -> int:
    """Wait until the page is quiet, at most ``max_ms``. Returns the ms waited.

    Quiet means: no XHR/fetch in flight, no visible loading indicator, and no
    DOM change for ``quiet_ms``. A page that is already quiet returns after one
    check, so a fast screen costs almost nothing.
    """
    begin = time.monotonic()
    deadline = begin + max(0, max_ms) / 1000
    while True:
        try:
            state = await page.evaluate(BUSY_JS)
        except Exception:
            state = None          # navigating: the next poll sees the new page
        if state is not None:
            still_for = state["now"] - (state["lastMutation"] or 0)
            if (state["ready"] and not state["spinner"] and not _requests_busy(page)
                    and still_for >= quiet_ms):
                break
        if time.monotonic() >= deadline:
            break
        try:
            await page.wait_for_timeout(min(100, quiet_ms))
        except Exception:
            break
    return int((time.monotonic() - begin) * 1000)
