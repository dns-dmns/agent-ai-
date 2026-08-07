"""Boots the real server in a throwaway workspace and drives the dashboard.

Catches the class of bug unit tests cannot: broken JS wiring, missing element
ids, endpoints the UI calls with the wrong parameters. Any console error or
unhandled exception on the page fails the run.

    .venv\\Scripts\\python.exe tests\\smoke_ui.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

WORKSPACE = Path(tempfile.mkdtemp(prefix="qa_smoke_"))
PROJECT = "SmokeTest"
FLOW = "demo master"

IGNORED_CONSOLE = (
    "favicon", "Failed to load resource: net::ERR_FILE_NOT_FOUND",
    "placeholder.png", "recording.webm",
    # The invalid-rule probe below deliberately provokes a 400 to prove the
    # server refuses bad input with a readable reason.
    "/save-rule",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed_workspace() -> None:
    (WORKSPACE / "web").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "web" / "dashboard.html", WORKSPACE / "web" / "dashboard.html")
    flow_dir = WORKSPACE / "projects" / PROJECT / "flows" / FLOW
    (flow_dir / "screenshots").mkdir(parents=True, exist_ok=True)

    (flow_dir / "flow.json").write_text(json.dumps({
        "flow_name": FLOW, "target_url": "https://example.test/master",
        "timestamp": "2026-07-31 09:00:00", "duration_seconds": 42, "step_count": 3,
    }), encoding="utf-8")
    (flow_dir / "actions.json").write_text(json.dumps([
        {"action": "TYPE", "selector": "#name", "value": "Demo", "tag": "input"},
        {"action": "CLICK", "selector": "#saveBtn", "tag": "button"},
    ]), encoding="utf-8")
    (flow_dir / "test_cases.json").write_text(json.dumps([
        {"scenario_name": "Create record", "type": "positive",
         "steps": [{"action": "TYPE", "selector": "#name", "value": "Demo"},
                   {"action": "CLICK", "selector": "#saveBtn"},
                   {"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": "Demo"}],
         "expected_result": "Record is created successfully and appears in the table."},
        {"scenario_name": "Empty name rejected", "type": "negative",
         "steps": [{"action": "CLICK", "selector": "#saveBtn"},
                   {"action": "VERIFY_MESSAGE", "value": "Name is required"}],
         "expected_result": "Validation error appears indicating the name is required."},
        {"scenario_name": "No assertion scenario", "type": "positive_ui",
         "steps": [{"action": "CLICK", "selector": "#saveBtn"}],
         "expected_result": "The dialog opens."},
    ], indent=2), encoding="utf-8")

    report = {
        "run_id": "smoke0001", "flow_name": FLOW, "project_name": PROJECT,
        "execution_date": "2026-07-31 09:30:00", "duration_seconds": 12,
        "test_types_run": "positive,negative", "mode": "headless", "workers": 2,
        "verdict_mode": "rules", "environment": "", "triggered_by": "dashboard",
        "total_test_cases": 3, "passed_count": 1, "failed_count": 1,
        "unverified_count": 1, "crash_count": 0, "cancelled": False,
        "console_errors": [], "network_errors": [],
        "checkpoints": [
            {"index": 1, "name": "Create record", "type": "positive", "status": "PASS",
             "verdict": {"status": "PASS", "reason": "Row 'Demo' found on the current page.",
                         "source": "assertions"},
             "screenshot": "", "video": "", "duration": 5, "steps_executed": [],
             "step_durations": [], "expected": "Record is created.", "messages": []},
            {"index": 2, "name": "Empty name rejected", "type": "negative", "status": "FAIL",
             "verdict": {"status": "FAIL",
                         "reason": "Expected a validation / error response, but the application showed none.",
                         "source": "rules"},
             "screenshot": "", "video": "", "duration": 4, "steps_executed": [],
             "step_durations": [], "expected": "Validation error appears.", "messages": [],
             "error": "Expected a validation error, none appeared.", "failed_step": 2},
            {"index": 3, "name": "No assertion scenario", "type": "positive_ui",
             "status": "UNVERIFIED",
             "verdict": {"status": "UNVERIFIED", "reason": "Nothing proved the expected outcome.",
                         "source": "rules"},
             "screenshot": "", "video": "", "duration": 3, "steps_executed": [],
             "step_durations": [], "expected": "The dialog opens.", "messages": []},
        ],
    }
    (flow_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    reports = flow_dir / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "report_1000.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    older = dict(report, execution_date="2026-07-30 09:30:00", passed_count=2,
                 failed_count=0, unverified_count=1)
    older["checkpoints"] = [dict(c, status="PASS" if c["index"] != 3 else "UNVERIFIED")
                            for c in report["checkpoints"]]
    (reports / "report_0900.json").write_text(json.dumps(older, indent=2), encoding="utf-8")
    (flow_dir / "run_history.json").write_text(json.dumps([
        {"run_id": "older", "stamp": 900, "execution_date": "2026-07-30 09:30:00",
         "duration_seconds": 11, "total": 3, "passed": 2, "failed": 0, "unverified": 1,
         "crashed": 0, "test_types": "", "mode": "headless", "verdict_mode": "rules",
         "triggered_by": "dashboard", "report_file": "reports/report_0900.json"},
        {"run_id": "smoke0001", "stamp": 1000, "execution_date": "2026-07-31 09:30:00",
         "duration_seconds": 12, "total": 3, "passed": 1, "failed": 1, "unverified": 1,
         "crashed": 0, "test_types": "", "mode": "headless", "verdict_mode": "rules",
         "triggered_by": "dashboard", "report_file": "reports/report_1000.json"},
    ], indent=2), encoding="utf-8")


async def main() -> int:
    from playwright.async_api import async_playwright

    seed_workspace()
    port = free_port()
    env = dict(os.environ, QA_WORKSPACE=str(WORKSPACE), QA_SCHEDULER="0", QA_LOG_LEVEL="WARNING")
    # Set to empty rather than popped: config.py calls load_dotenv(), which
    # fills in anything ABSENT from the environment — so popping the key let a
    # developer's .env put it back, and these suites only ran offline on CI.
    env["GEMINI_API_KEY"] = ""

    server = subprocess.Popen(
        # sys.executable keeps this working on Linux CI as well as a Windows venv.
        [sys.executable, "-m", "uvicorn", "qa_agent:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    print(f"Server starting on {base} (workspace {WORKSPACE})")

    try:
        import urllib.request

        for _ in range(60):
            if server.poll() is not None:
                print("Server exited early:\n" + (server.stdout.read() if server.stdout else ""))
                return 1
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=1) as response:
                    print("Health:", json.loads(response.read()))
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("Server never became healthy.")
            return 1

        problems: list[str] = []
        checks: list[tuple[str, bool, str]] = []

        def record(name: str, ok: bool, detail: str = "") -> None:
            checks.append((name, ok, detail))
            if not ok:
                problems.append(f"{name}: {detail}")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            console_errors: list[str] = []
            def on_console(msg) -> None:
                if msg.type != "error":
                    return
                where = (msg.location or {}).get("url", "")
                blob = f"{msg.text} @ {where}"
                if any(ignored in blob for ignored in IGNORED_CONSOLE):
                    return
                console_errors.append(blob)

            page.on("console", on_console)
            page.on("pageerror", lambda err: console_errors.append(f"PAGEERROR: {err}"))

            await page.goto(base, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)

            # Landing overlay → pick the seeded project
            await page.select_option("#landerProjectSelect", PROJECT)
            await page.click("button:has-text('Open workspace')")
            await page.wait_for_timeout(900)
            record("project loaded", await page.locator("#projectLander").is_hidden())

            status_text = await page.locator("#platformStatus").inner_text()
            # Case-insensitive: app.js writes "no API key" and the ui.js redesign
            # writes "No API key". Matching the old casing meant this only ever
            # passed where a key was configured — so it went green locally and red
            # on CI, which has none.
            lowered = status_text.lower()
            record("platform status bar",
                   "ai ready" in lowered or "no api key" in lowered,
                   repr(status_text))

            # Sidebar lists the flows and drives the hidden <select> the logic reads
            record("sidebar lists flows",
                   await page.locator("#sidebarFlowList .row").count() >= 1)
            await page.click(f"#sidebarFlowList .row:has-text('{FLOW}')")
            await page.wait_for_timeout(1500)
            record("breadcrumb follows selection",
                   FLOW in (await page.locator("#crumbFlow").inner_text()))

            # Rail step 3 holds the suite and execution profile
            await page.click(".rail-step:has-text('Run & verify')")
            await page.wait_for_timeout(600)

            record("suite rendered", await page.locator("#suiteCard-1").count() > 0)
            record("assertion warning shown",
                   "no VERIFY step" in (await page.locator("#suiteList").inner_text()))
            record("unverified tile restored",
                   (await page.locator("#statUnverified").inner_text()) == "1")
            await page.click(".rail-step:has-text('Report')")
            await page.wait_for_timeout(500)
            record("report badge reflects failure",
                   (await page.locator("#reportStatusBadge").inner_text()).strip() == "FAIL")
            record("badge is a semantic status chip",
                   "fail" in (await page.get_attribute("#reportStatusBadge", "class") or ""))
            await page.click(".rail-step:has-text('Run & verify')")
            await page.wait_for_timeout(400)
            card_verdict = await page.locator("#suiteCardVerdict-2").inner_text()
            record("verdict reason on card", "showed none" in card_verdict, card_verdict)
            verdict_notes = await page.locator("#verdictLogList").inner_text()
            record("verdict notes list", "#2 FAIL" in verdict_notes and "#3 UNVERIFIED" in verdict_notes)

            # Verdict engine is a primary control now, evidence options are disclosed
            record("verdict control visible", await page.locator("#verdictModeSelect").is_visible())
            await page.click("summary:has-text('Evidence & data options')")
            await page.wait_for_timeout(300)
            record("evidence options open", await page.locator("#stepShotsSelect").is_visible())
            record("env options loaded", await page.locator("#envSelect").count() > 0)

            # Search + selection + selective-run button
            await page.fill("#suiteSearchInput", "empty")
            await page.wait_for_timeout(300)
            record("search filters cards", await page.locator("#suiteCard-1").is_hidden())
            await page.fill("#suiteSearchInput", "")
            await page.wait_for_timeout(200)
            await page.check(".caseSelect >> nth=0")
            await page.wait_for_timeout(200)
            record("run-selected button appears",
                   await page.locator("#runSelectedBtn").is_visible())
            await page.uncheck(".caseSelect >> nth=0")

            # Scenario editor
            await page.click("#suiteCard-1 button[title='Edit scenario']")
            await page.wait_for_timeout(400)
            editor_text = await page.locator("#caseEditorText").input_value()
            record("editor prefilled", "Create record" in editor_text)
            await page.click("#genericModalFooter button:has-text('Cancel')")
            await page.wait_for_timeout(200)

            # Run history + comparison
            await page.click(".sidebar-footer .row:has-text('Run history')")
            await page.wait_for_timeout(600)
            history_text = await page.locator("#genericModalBody").inner_text()
            record("history listed", "2026-07-31 09:30:00" in history_text)
            await page.select_option("#compareA", "reports/report_0900.json")
            await page.select_option("#compareB", "reports/report_1000.json")
            await page.click("#compareRunsBtn")
            await page.wait_for_timeout(700)
            compare_text = await page.locator("#compareResult").inner_text()
            record("comparison detects regression", "Newly failing" in compare_text, compare_text)
            await page.click("#genericModalClose")
            await page.wait_for_timeout(200)

            # Project settings
            await page.click(".sidebar-footer .row:has-text('Settings')")
            await page.wait_for_timeout(600)
            record("settings modal", await page.locator("#settingsEnvJson").count() > 0)
            await page.click("#genericModalFooter button:has-text('Cancel')")
            await page.wait_for_timeout(200)

            # Schedules
            await page.click(".sidebar-footer .row:has-text('Schedules')")
            await page.wait_for_timeout(600)
            record("schedule modal", await page.locator("#schedName").count() > 0)
            await page.fill("#schedName", "Smoke nightly")
            await page.fill("#schedAt", "03:15")
            await page.click("#genericModalBody button:has-text('Add schedule')")
            await page.wait_for_timeout(800)
            record("schedule saved",
                   "Smoke nightly" in (await page.locator("#genericModalBody").inner_text()))
            await page.click("#genericModalClose")
            await page.wait_for_timeout(200)

            # Flaky report
            await page.click(".sidebar-footer .row:has-text('Flaky')")
            await page.wait_for_timeout(600)
            record("flaky modal", "Flaky scenarios" in
                   (await page.locator("#genericModalTitle").inner_text()))
            await page.click("#genericModalFooter button:has-text('Close')")
            await page.wait_for_timeout(200)

            # Console still works
            await page.click(".rail-step:has-text('Console')")
            await page.wait_for_timeout(900)
            record("project console renders",
                   await page.locator("#project-view").is_visible())

            # ── Rules surface ─────────────────────────────────────────
            await page.click(".sidebar-footer .row:has-text('Rules')")
            await page.wait_for_timeout(700)
            record("rules manager opens", await page.locator("#ruleText").count() > 0)
            await page.click("#genericModalFooter button:has-text('Add manually')")
            await page.wait_for_timeout(400)
            editor = await page.locator("#ruleEditJson").input_value()
            record("rule editor prefilled", "must_show" in editor)

            await page.fill("#ruleEditJson", json.dumps({
                "title": "Smoke rule", "kind": "field_rejects",
                "selector": "#name", "values": ["0"], "severity": "major",
                "generate_scenario": False,
            }, indent=2))
            await page.click("#genericModalFooter button:has-text('Save rule')")
            await page.wait_for_timeout(900)
            record("rule saved and listed",
                   "Smoke rule" in (await page.locator("#genericModalBody").inner_text()))
            record("rules badge updates",
                   (await page.locator("#rulesCountBadge").inner_text()).strip() != "")

            # An invalid rule must be refused with a readable reason.
            await page.click("#genericModalFooter button:has-text('Add manually')")
            await page.wait_for_timeout(400)
            await page.fill("#ruleEditJson", '{"kind": "field_rejects", "title": "bad"}')
            await page.click("#genericModalFooter button:has-text('Save rule')")
            await page.wait_for_timeout(700)
            record("invalid rule refused with a reason",
                   "selector" in (await page.locator("#ruleEditError").inner_text()))
            await page.click("#genericModalFooter button:has-text('Back')")
            await page.wait_for_timeout(600)
            await page.click("#genericModalClose")
            await page.wait_for_timeout(300)

            # ── Project export: read-only, generates a Playwright suite ──
            await page.click(".sidebar-footer .row:has-text('Export project')")
            await page.wait_for_timeout(1200)
            export_body = await page.locator("#genericModalBody").text_content() or ""
            record("export preview opens and lists files",
                   "modules" in export_body.lower() and "files" in export_body.lower(),
                   export_body[:150])
            record("the generated page object is previewable",
                   "class" in export_body and "BasePage" in export_body,
                   export_body[:200])
            await page.click("#genericModalClose")
            await page.wait_for_timeout(300)

            # ── Coverage surface: scenarios, test cases, named gaps ───
            # The badge is the fastest signal that a suite is thin, so it has to
            # be populated by the time a module finishes loading.
            badge = (await page.locator("#coverageBadge").inner_text()).strip()
            record("coverage badge is populated", badge not in ("", "—"), badge)

            await page.click(".sidebar-footer .row:has-text('Coverage')")
            await page.wait_for_timeout(800)
            body = await page.locator("#genericModalBody").inner_text()
            record("coverage report opens", "test cases" in body.lower(), body[:120])
            record("coverage names the behaviours with no test case",
                   "Behaviours with no test case" in body, body[:120])
            # The gap that mattered: nothing in the suite opens an audit trail.
            record("audit history is reported as a gap",
                   "Audit / history" in body, body[:200])

            await page.click("#genericModalFooter button:has-text('Enumerate field cases')")
            await page.wait_for_timeout(1200)
            # text_content(), not inner_text(): the per-field tables live inside
            # collapsed <details>, and inner_text() only returns rendered text.
            enumerated = await page.locator("#genericModalBody").text_content() or ""
            title = await page.locator("#genericModalTitle").inner_text()
            record("field cases are enumerated without an API key",
                   "Enumerated" in title and "test cases" in enumerated.lower()
                   and "single character is accepted" in enumerated,
                   f"title={title!r} len={len(enumerated)} body={enumerated[:200]!r}")
            await page.click("#genericModalClose")
            await page.wait_for_timeout(300)

            # ── Dependency contract surface ───────────────────────────
            await page.click(".sidebar-footer .row:has-text('Dependencies')")
            await page.wait_for_timeout(700)
            record("contract editor opens", await page.locator("#contractJson").count() > 0)
            await page.fill("#contractJson", json.dumps({
                "provides": [], "run_once": True,
                "requires": [{"entity": "category", "selector": "#cat", "kind": "dropdown"}],
            }, indent=2))
            await page.click("#genericModalFooter button:has-text('Save contract')")
            await page.wait_for_timeout(900)
            record("contract saved",
                   "category" in (await page.locator("#contractJson").input_value()))
            await page.click("#genericModalFooter button:has-text('Preview checks')")
            await page.wait_for_timeout(800)
            preview = await page.locator("#genericModalBody").inner_text()
            record("preview explains the missing provider",
                   "No chained prerequisite provides" in preview, preview[:120])
            await page.click("#genericModalFooter button:has-text('Back')")
            await page.wait_for_timeout(500)
            await page.click("#genericModalClose")
            await page.wait_for_timeout(300)

            # ── New execution options ─────────────────────────────────
            await page.click(".rail-step:has-text('Run & verify')")
            await page.wait_for_timeout(400)
            record("new run options present",
                   await page.locator("#seedOnceCheck").count() > 0 and
                   await page.locator("#relationshipCheck").count() > 0 and
                   await page.locator("#applyRulesCheck").count() > 0)

            # ── Redesign-specific behaviour ───────────────────────────
            theme_before = await page.get_attribute("html", "data-theme")
            await page.click("#themeToggle")
            await page.wait_for_timeout(400)
            record("theme toggle flips appearance",
                   await page.get_attribute("html", "data-theme") != theme_before)

            await page.keyboard.press("Control+k")
            await page.wait_for_timeout(500)
            record("command palette opens",
                   await page.locator("#paletteList .row").count() > 5)
            await page.fill("#paletteInput", "report")
            await page.wait_for_timeout(300)
            record("command palette filters",
                   await page.locator("#paletteList .row").count() >= 1)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            record("palette closes on Escape",
                   not await page.locator("#paletteScrim").is_visible())

            record("workflow rail marks completed steps",
                   await page.locator(".rail-step.done").count() >= 1)

            await browser.close()

        # Server-side endpoints used by the export buttons
        for path, marker in (
            (f"/export-report-html?project_name={PROJECT}&flow_name={FLOW}", "QA Execution Report"),
            (f"/export-junit?project_name={PROJECT}&flow_name={FLOW}", "<testsuite"),
            (f"/export-report-csv?project_name={PROJECT}&flow_name={FLOW}", "Scenario"),
            (f"/export-test-matrix?project_name={PROJECT}&flow_name={FLOW}", "Pre-condition"),
            (f"/export-test-matrix?project_name={PROJECT}&flow_name={FLOW}&fmt=markdown",
             "Test Case Matrix"),
            # The matrix now separates the objective from the concrete case.
            (f"/export-test-matrix?project_name={PROJECT}&flow_name={FLOW}", "Test Case"),
            # Coverage: countable scenarios, test cases and named gaps.
            (f"/coverage-report?project_name={PROJECT}&flow_name={FLOW}", "checklist_percent"),
            (f"/coverage-report?project_name={PROJECT}&flow_name={FLOW}", "test_cases"),
        ):
            url = base + path.replace(" ", "%20")
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    body = response.read().decode("utf-8", "replace")
                record(f"GET {path.split('?')[0]}", marker in body, body[:120])
            except Exception as err:
                record(f"GET {path.split('?')[0]}", False, str(err))

        print("\n" + "=" * 78)
        for name, ok, detail in checks:
            print(f"{'OK' if ok else 'XX'}  {name}" + (f"  → {detail[:70]}" if not ok else ""))
        print("=" * 78)

        if console_errors:
            print("\nCONSOLE ERRORS:")
            for err in console_errors[:20]:
                print("  -", err)

        ok = not problems and not console_errors
        print("\n" + ("UI SMOKE TEST PASSED ✅" if ok else "UI SMOKE TEST FAILED ❌"))
        return 0 if ok else 1
    finally:
        # terminate() can leave the socket held on Windows, which then breaks the
        # next manual `uvicorn` start with WinError 10048. Always confirm it died.
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
            try:
                server.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(WORKSPACE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
