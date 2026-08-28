"""End-to-end test of the injected recorder, in a real browser.

Every other recorder test in this repo is a string check against
``app/recorder.py``. That cannot catch the failures that actually matter here:
the script depends on ``getBoundingClientRect`` and ``getComputedStyle``, so it
only behaves in a browser, and its two riskiest behaviours — that assert mode
suppresses the click, and that a password never reaches a captured key — are
invisible to a grep.

    .venv\\Scripts\\python.exe tests\\e2e_recorder.py

Needs no Gemini key and never touches real project data.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Throwaway workspace, set before importing config (see e2e_selftest.py).
os.environ["QA_WORKSPACE"] = tempfile.mkdtemp(prefix="qa_recorder_")
os.environ["QA_SCHEDULER"] = "0"
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright   # noqa: E402
from app import recorder                            # noqa: E402
from app import credentials                         # noqa: E402

# A deliberately misspelled heading: "Custmoer" is the whole point of assert
# mode — a spelling mistake should become a test failure, not something a human
# has to notice.
PAGE = """
<!doctype html><html><body>
  <h1 id="pageTitle">Custmoer Master</h1>
  <label for="custName">Customer Name</label>
  <input id="custName" name="custName" placeholder="Enter name">
  <label for="pwd">Password</label>
  <input id="pwd" name="password" type="password">
  <select id="status"><option>Active</option><option>Inactive</option></select>
  <button id="saveBtn" onclick="document.getElementById('out').textContent='clicked'">Save</button>
  <a href="#next" id="nextLink">Go to next page</a>
  <div id="out"></div>
</body></html>
"""

SECRET = "SuperSecret123"

events: list[dict] = []
failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label}  {detail}")
        failures.append(label)


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.expose_function("recordPlaywrightEvent", lambda ev: events.append(ev))
        await page.set_content(PAGE)
        await page.evaluate(recorder.JS_RECORDER_SCRIPT)

        # ── Normal recording still works, and now carries codegen data ──
        await page.click("#saveBtn")
        await page.wait_for_timeout(150)
        save = next((e for e in events if e.get("selector") == "#saveBtn"), None)
        check("a click is still recorded", save is not None)
        if save:
            check("the ARIA role is computed", save.get("role") == "button",
                  save.get("role"))
            check("the accessible name is captured",
                  save.get("accessible_name") == "Save", save.get("accessible_name"))
            check("the full text is captured", save.get("text_full") == "Save",
                  save.get("text_full"))
            check("an exact text= locator is offered",
                  'text="Save"' in (save.get("selectors") or []), save.get("selectors"))
            check("the recorded selector is still the stable id",
                  save.get("selector") == "#saveBtn", save.get("selector"))
        check("the click reached the application", await page.inner_text("#out") == "clicked")

        events.clear()
        await page.click("#nextLink")
        await page.wait_for_timeout(150)
        link = next((e for e in events if e.get("tag") == "a"), None)
        check("a link is recorded with role=link", link and link.get("role") == "link",
              link and link.get("role"))
        check("its href is captured for review",
              link and (link.get("attrs") or {}).get("href", "").endswith("#next"),
              link and link.get("attrs"))

        # ── A password must not reach ANY captured key ──────────────────
        # credentials.scrub_recorded_actions cleans `value` and `text`. A second
        # copy under text_full or accessible_name would walk straight past it
        # and into actions.json, which is committed.
        events.clear()
        await page.fill("#pwd", SECRET)
        await page.click("#saveBtn")
        await page.wait_for_timeout(200)
        leaked = [e for e in events
                  if SECRET in json.dumps({k: v for k, v in e.items() if k != "value"})]
        check("no captured key other than value echoes the password", not leaked, leaked)
        pwd_ev = next((e for e in events if e.get("selector") == "#pwd"), None)
        if pwd_ev:
            check("the password field's text_full is blanked", not pwd_ev.get("text_full"))

        # And the scrubber still removes the one legitimate copy.
        scrubbed = list(events)
        credentials.scrub_recorded_actions(scrubbed, "RecorderTest")
        check("after scrubbing, the password is gone entirely",
              SECRET not in json.dumps(scrubbed))

        # ── Assert mode ─────────────────────────────────────────────────
        await page.evaluate("document.getElementById('out').textContent = '';")
        await page.evaluate("window.__qaAssertMode = true;")

        events.clear()
        await page.click("#pageTitle")
        await page.wait_for_timeout(150)
        assertion = next((e for e in events if e.get("asserted")), None)
        check("assert mode records an assertion", assertion is not None)
        if assertion:
            check("it is a VERIFY_TEXT step",
                  assertion.get("action") == "VERIFY_TEXT", assertion.get("action"))
            check("it carries the exact on-screen wording, typo included",
                  assertion.get("value") == "Custmoer Master", assertion.get("value"))

        events.clear()
        await page.click("#saveBtn")
        await page.wait_for_timeout(200)
        check("an assert-mode click does NOT drive the application",
              await page.inner_text("#out") == "",
              f"out={await page.inner_text('#out')!r}")
        check("and records no CLICK step",
              not any(e.get("action") == "CLICK" for e in events),
              [e.get("action") for e in events])

        events.clear()
        await page.evaluate("document.getElementById('custName').value = 'Acme Ltd';")
        await page.click("#custName")
        await page.wait_for_timeout(150)
        val = next((e for e in events if e.get("asserted")), None)
        check("asserting a text field yields VERIFY_VALUE",
              val and val.get("action") == "VERIFY_VALUE", val and val.get("action"))
        check("carrying the field's current value",
              val and val.get("value") == "Acme Ltd", val and val.get("value"))

        events.clear()
        await page.click("#pwd")
        await page.wait_for_timeout(150)
        pw_assert = next((e for e in events if e.get("asserted")), None)
        check("asserting a password degrades to VERIFY_VISIBLE",
              pw_assert and pw_assert.get("action") == "VERIFY_VISIBLE",
              pw_assert and pw_assert.get("action"))
        check("so the expected value is never the credential",
              SECRET not in json.dumps(events))

        # ── Back to normal ──────────────────────────────────────────────
        await page.evaluate("window.__qaAssertMode = false;")
        events.clear()
        await page.click("#saveBtn")
        await page.wait_for_timeout(200)
        check("switching off restores normal recording",
              await page.inner_text("#out") == "clicked")
        check("and a CLICK is recorded again",
              any(e.get("action") == "CLICK" for e in events))

        await browser.close()

    print()
    print("=" * 78)
    print(f"{checks - len(failures)}/{checks} recorder checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    print("=" * 78)
    print("RECORDER TEST PASSED ✅" if not failures else "RECORDER TEST FAILED ❌")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
