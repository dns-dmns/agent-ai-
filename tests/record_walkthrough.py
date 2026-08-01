"""Records a narrated walkthrough video of the platform.

Drives the real dashboard against the real server and records it, producing a
"how to use this" video with on-screen captions. Nothing is faked — every panel
shown is the live application.

    .venv\\Scripts\\python.exe tests\\record_walkthrough.py --project ornage --flow Login

Output: walkthrough/walkthrough.webm  (plus numbered stills)
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "walkthrough"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# The caption bar is injected into the page so the video explains itself.
CAPTION_CSS = """
#wtCaption {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 99999;
    background: linear-gradient(0deg, rgba(5,7,12,.97), rgba(5,7,12,.86));
    color: #f4f6fb; font: 500 19px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
    padding: 18px 30px 20px; border-top: 2px solid #5b8cff;
    display: flex; align-items: baseline; gap: 16px;
    box-shadow: 0 -12px 40px rgba(0,0,0,.5);
}
#wtStep {
    font: 700 13px/1 ui-monospace, monospace; letter-spacing: .12em;
    color: #05070c; background: #5b8cff; padding: 7px 11px; border-radius: 999px;
    white-space: nowrap;
}
#wtText b { color: #7ba1ff; }
#wtCursor {
    position: fixed; width: 26px; height: 26px; border-radius: 50%;
    border: 3px solid #5b8cff; background: rgba(91,140,255,.25);
    z-index: 99998; pointer-events: none; transition: all .45s cubic-bezier(.32,.72,0,1);
    transform: translate(-50%, -50%); opacity: 0;
}
"""

CAPTION_JS = """(text) => {
    let bar = document.getElementById('wtCaption');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'wtCaption';
        bar.innerHTML = '<span id="wtStep"></span><span id="wtText"></span>';
        document.body.appendChild(bar);
        const dot = document.createElement('div');
        dot.id = 'wtCursor';
        document.body.appendChild(dot);
    }
    const [step, body] = text;
    document.getElementById('wtStep').textContent = step;
    document.getElementById('wtText').innerHTML = body;
}"""

POINT_JS = """(box) => {
    const dot = document.getElementById('wtCursor');
    if (!dot || !box) return;
    dot.style.opacity = '1';
    dot.style.left = box.x + 'px';
    dot.style.top = box.y + 'px';
}"""


class Recorder:
    def __init__(self, page, shots: Path):
        self.page = page
        self.shots = shots
        self.n = 0

    async def say(self, step: str, text: str, hold: float = 3.2) -> None:
        await self.page.evaluate(CAPTION_JS, [step, text])
        await self.page.wait_for_timeout(int(hold * 1000))
        self.n += 1
        try:
            await self.page.screenshot(path=str(self.shots / f"{self.n:02d}.png"))
        except Exception:
            pass
        print(f"  {step}  {text[:78]}")

    async def point_at(self, selector: str) -> None:
        try:
            box = await self.page.locator(selector).first.bounding_box()
            if box:
                await self.page.evaluate(
                    POINT_JS, {"x": box["x"] + box["width"] / 2,
                               "y": box["y"] + box["height"] / 2})
                await self.page.wait_for_timeout(600)
        except Exception:
            pass

    async def click(self, selector: str, pause: float = 1.2) -> None:
        await self.point_at(selector)
        try:
            await self.page.click(selector, timeout=8000)
        except Exception as err:
            print(f"     (skipped click on {selector}: {str(err)[:60]})")
            return
        await self.page.wait_for_timeout(int(pause * 1000))


async def main(base: str, project: str, flow: str) -> int:
    from playwright.async_api import async_playwright

    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    shots = OUT / "stills"
    shots.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 940},
            record_video_dir=str(OUT), record_video_size={"width": 1600, "height": 940},
            color_scheme="dark",
        )
        page = await context.new_page()
        await page.add_style_tag(content=CAPTION_CSS) if False else None
        show = Recorder(page, shots)

        async def style() -> None:
            try:
                await page.add_style_tag(content=CAPTION_CSS)
            except Exception:
                pass

        # ── Opening ───────────────────────────────────────────────────
        await page.goto(base, wait_until="domcontentloaded")
        await page.wait_for_timeout(1800)
        await style()
        await show.say("WELCOME", "The <b>AI QA Automation Platform</b> — record a flow once, "
                                 "let AI agents write the suite, then verify it for real.", 4)
        await show.say("STEP 0", "Everything lives in a <b>workspace</b>. Pick one to begin.", 3)

        await page.select_option("#landerProjectSelect", project)
        await show.click("button:has-text('Open workspace')", 2)
        await style()

        await show.say("THE LAYOUT", "Left: your <b>flows</b>. Top: the workflow — "
                                     "<b>Record → Generate → Run &amp; verify → Report</b>.", 4.5)
        await show.point_at(".rail")

        # ── 1. Record ─────────────────────────────────────────────────
        await show.say("1 · RECORD", "Enter a flow name and start URL, click <b>Launch browser</b>, "
                                     "then use the app exactly as a tester would.", 4.5)
        await show.point_at("#flowNameInput")
        await show.say("1 · RECORD", "Every click, keystroke and dropdown is captured — each with "
                                     "<b>several fallback selectors</b>, so replays survive UI changes.", 4.5)

        # ── Open the recorded flow ────────────────────────────────────
        await show.click(f"#sidebarFlowList .row:has-text('{flow}')", 2.5)
        await style()
        await show.say("CAPTURED", f"This is the recorded <b>{flow}</b> flow. Each step kept its "
                                   f"screenshot, so you can see what was captured.", 4)

        # ── 2. Generate ───────────────────────────────────────────────
        await show.click(".rail-step:has-text('Generate')", 1.5)
        await style()
        await show.say("2 · GENERATE", "Three agents work on the recording: the <b>Builder</b> writes "
                                       "functional coverage, the <b>Breaker</b> attacks it, the "
                                       "<b>Auditor</b> reviews both and fills the gaps.", 5)
        await show.point_at("#multiAgentBtn")
        await show.say("2 · GENERATE", "Tick the test types you want, then <b>Multi-agent generate</b>. "
                                       "You can also import a JSON suite you already have.", 4.5)

        # ── Rules ─────────────────────────────────────────────────────
        await show.click(".sidebar-footer .row:has-text('Rules')", 2)
        await style()
        await show.say("RULES", "Things you know that no recording can show — written in "
                               "<b>plain English</b>.", 4)
        await show.say("RULES", "“A failed login must never leave the login page.”<br>"
                               "“Beam Length must reject 0 and negative values.”", 4.5)
        await show.say("RULES", "Each rule is <b>enforced on every scenario</b>, fed into the "
                               "agents, and can generate its own test case.", 4.5)
        await show.click("#genericModalClose", 1)

        # ── Dependencies ──────────────────────────────────────────────
        await show.click(".sidebar-footer .row:has-text('Dependencies')", 2)
        await style()
        await show.say("DEPENDENCIES", "A <b>contract</b> says what a module provides and requires. "
                                       "A master's value is then captured and reused downstream as "
                                       "<b>{{variable}}</b> — never a hardcoded guess.", 5.5)
        await show.click("#genericModalClose", 1)

        # ── 3. Run & verify ───────────────────────────────────────────
        await show.click(".rail-step:has-text('Run & verify')", 1.5)
        await style()
        await show.say("3 · RUN", "Choose how many scenarios, headed or headless, how many "
                                  "<b>parallel workers</b>, and which verdict engine.", 4.5)
        await show.point_at("#verdictModeSelect")
        await show.say("3 · RUN", "<b>Headed</b> shows you the browser. <b>Headless</b> runs in the "
                                  "background — up to 5 scenarios at once.", 4.5)

        await show.say("THE KEY IDEA", "A scenario used to “pass” whenever no step crashed. "
                                       "So a negative test passed by typing bad data and clicking Save.", 5)
        await show.say("THE KEY IDEA", "Now each one is judged against its expected result using "
                                       "<b>evidence from the page</b> — messages, dialogs, rows, "
                                       "downloads, assertions.", 5)
        await show.point_at("#suiteList")
        await show.say("3 · RUN", "Every scenario shows its steps and its verdict reason. "
                                  "Tick some and use <b>Run selected</b>, or run the lot.", 4.5)

        # ── 4. Report ─────────────────────────────────────────────────
        await show.click(".rail-step:has-text('Report')", 2)
        await style()
        await show.say("4 · REPORT", "Four outcomes: <b>PASS</b>, <b>FAIL</b>, "
                                     "<b>UNVERIFIED</b> and <b>CRASH</b>.", 4.5)
        await show.point_at(".metrics")
        await show.say("UNVERIFIED", "“Every step ran, but nothing proved it.” That is not a pass — "
                                     "it tells you to add an assertion.", 5)
        await show.say("4 · REPORT", "Screenshots, video and traces per scenario, plus exports: "
                                     "<b>PDF, CSV, JUnit XML, evidence ZIP</b> and the "
                                     "<b>Test Case Matrix</b>.", 5)

        # ── Wrap up ───────────────────────────────────────────────────
        await show.click(".sidebar-footer .row:has-text('Run history')", 2)
        await style()
        await show.say("HISTORY", "Every run is kept. <b>Compare</b> two runs to see exactly which "
                                  "scenarios flipped.", 4.5)
        await show.click("#genericModalClose", 1)

        await show.say("ALSO", "Nightly <b>schedules</b> with webhooks · <b>flaky</b> detection · "
                              "CI/CD trigger endpoints · <b>Ctrl K</b> for anything.", 5)
        await show.say("THAT'S IT", "Record once · generate with AI · <b>verify with evidence</b> · "
                                    "ship with proof.", 5)

        await context.close()
        await browser.close()

    videos = list(OUT.glob("*.webm"))
    if videos:
        final = OUT / "walkthrough.webm"
        videos[0].replace(final)
        size = final.stat().st_size / (1024 * 1024)
        print(f"\nVideo : {final}  ({size:.1f} MB)")
        print(f"Stills: {shots} ({len(list(shots.glob('*.png')))} frames)")
        return 0
    print("No video produced.")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--project", default="ornage")
    parser.add_argument("--flow", default="Login")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.base, args.project, args.flow)))
