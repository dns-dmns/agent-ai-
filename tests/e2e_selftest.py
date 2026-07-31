"""End-to-end self test of the execution engine against a local sample app.

This runs the *real* Playwright runner against ``tests/selftest_app`` in a
throwaway workspace, then asserts that each scenario reached the verdict it is
supposed to reach. It needs no Gemini key and never touches real project data.

    .venv\\Scripts\\python.exe tests\\e2e_selftest.py

Every scenario below is chosen to prove one specific capability, including the
big one: a negative test whose validation never appears must come back FAIL,
not PASS.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP_DIR = HERE / "selftest_app"

# Point the platform at a throwaway workspace *before* importing config.
WORKSPACE = Path(tempfile.mkdtemp(prefix="qa_selftest_"))
os.environ["QA_WORKSPACE"] = str(WORKSPACE)
os.environ["QA_SCHEDULER"] = "0"
os.environ.pop("GEMINI_API_KEY", None)          # keep the run fully offline
sys.path.insert(0, str(ROOT))

from app import config      # noqa: E402
from app import executor    # noqa: E402

PROJECT = "SelfTest"
FLOW = "widget master"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve_app(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(APP_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


LONG_NAME = "N" * 80


def build_suite(base_url: str) -> list[dict]:
    """Each scenario carries ``_expect`` — the verdict the engine must produce."""
    return [
        {
            "scenario_name": "Create widget with valid data",
            "type": "positive",
            "_expect": "PASS",
            "_proves": "TYPE + SELECT + success toast + row verification",
            "steps": [
                {"action": "TYPE", "selector": "#name", "value": "Alpha Widget"},
                {"action": "TYPE", "selector": "#code", "value": "AW900"},
                {"action": "TYPE", "selector": "#qty", "value": "7"},
                {"action": "SELECT", "selector": "#status", "value": "Active"},
                {"action": "CHECK", "selector": "#featured"},
                {"action": "CLICK", "selector": "#saveBtn"},
                {"action": "VERIFY_MESSAGE", "value": "saved successfully"},
                {"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": "AW900"},
            ],
            "expected_result": "Widget is created successfully and appears in the grid.",
        },
        {
            "scenario_name": "Empty name is rejected",
            "type": "negative",
            "_expect": "PASS",
            "_proves": "negative case where the validation really does appear",
            "steps": [
                {"action": "TYPE", "selector": "#code", "value": "EMPTY1"},
                {"action": "CLICK", "selector": "#saveBtn"},
                {"action": "VERIFY_MESSAGE", "value": "Name is required"},
            ],
            "expected_result": "Validation error appears indicating the name is required.",
        },
        {
            "scenario_name": "Duplicate code is rejected",
            "type": "negative",
            "_expect": "PASS",
            "_proves": "duplicate-key validation detected from a toast",
            "steps": [
                {"action": "TYPE", "selector": "#name", "value": "Dupe Widget"},
                {"action": "TYPE", "selector": "#code", "value": "W001"},
                {"action": "CLICK", "selector": "#saveBtn"},
                {"action": "VERIFY_MESSAGE", "value": "already exists"},
            ],
            "expected_result": "System displays a duplicate record error preventing the save.",
        },
        {
            "scenario_name": "Name exceeding 50 characters is rejected",
            "type": "boundary",
            "_expect": "FAIL",
            "_proves": "THE BIG ONE — app silently accepts oversized input, so the "
                       "old runner reported PASS. No validation appeared ⇒ FAIL.",
            "steps": [
                {"action": "TYPE", "selector": "#name", "value": LONG_NAME},
                {"action": "TYPE", "selector": "#code", "value": "LONG1"},
                {"action": "CLICK", "selector": "#saveBtn"},
            ],
            "expected_result": "Validation error appears because the name exceeds the "
                               "maximum allowed length.",
        },
        {
            "scenario_name": "Cancel delete confirmation keeps the record",
            "type": "positive",
            "_expect": "PASS",
            "_proves": "native confirm() dismissed on purpose (DIALOG verb)",
            "steps": [
                {"action": "DIALOG", "value": "dismiss"},
                {"action": "CLICK", "selector": "button.delBtn[data-code='W003']"},
                {"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": "W003"},
            ],
            "expected_result": "Delete confirmation is cancelled and the record remains "
                               "in the table.",
        },
        {
            "scenario_name": "Confirming delete removes the record",
            "type": "positive",
            "_expect": "PASS",
            "_proves": "native confirm() accepted — impossible before, Playwright "
                       "auto-dismissed it and the delete silently never happened",
            "steps": [
                {"action": "DIALOG", "value": "accept"},
                {"action": "CLICK", "selector": "button.delBtn[data-code='W004']"},
                {"action": "VERIFY_MESSAGE", "value": "deleted successfully"},
                {"action": "VERIFY_ROW_ABSENT", "selector": "table", "value": "W004"},
            ],
            "expected_result": "Record is deleted successfully and removed from the table.",
        },
        {
            "scenario_name": "Export downloads every record",
            "type": "positive",
            "_expect": "PASS",
            "_proves": "download capture + row counting (Ambica bug #1 pattern)",
            "steps": [
                {"action": "CLICK", "selector": "#exportBtn"},
                {"action": "WAIT", "value": "800"},
                {"action": "VERIFY_DOWNLOAD", "value": "at least 10 rows"},
            ],
            "expected_result": "An Excel/CSV file is downloaded containing all records.",
        },
        {
            "scenario_name": "Navigate to the widget detail page",
            "type": "positive",
            "_expect": "PASS",
            "_proves": "NAVIGATE + VERIFY_URL + VERIFY_TEXT",
            "steps": [
                {"action": "NAVIGATE", "selector": f"{base_url}/detail.html"},
                {"action": "VERIFY_URL", "value": "detail.html"},
                {"action": "VERIFY_TEXT", "selector": "#heading", "value": "Widget Detail"},
            ],
            "expected_result": "The widget detail page is displayed.",
        },
        {
            "scenario_name": "Record on the second table page is found",
            "type": "positive",
            "_expect": "PASS",
            "_proves": "pagination/search aware row verification",
            "steps": [
                {"action": "VERIFY_ROW_CONTAINS", "selector": "table",
                 "value": "Page Two Widget"},
            ],
            "expected_result": "The record is listed in the grid.",
        },
        {
            "scenario_name": "Search box can be focused",
            "type": "positive_ui",
            "_expect": "UNVERIFIED",
            "_proves": "honest reporting — steps ran but nothing proved the outcome",
            "steps": [
                {"action": "CLICK", "selector": "#search"},
                {"action": "KEYPRESS", "selector": "#search", "value": "Enter"},
            ],
            "expected_result": "The search box is displayed and ready for input.",
        },
        {
            "scenario_name": "XSS payload in notes is stored safely",
            "type": "security",
            "_expect": "FAIL",
            "_proves": "injected payload actually executed ⇒ real security defect",
            "steps": [
                {"action": "TYPE", "selector": "#bio",
                 "value": "<img src=x onerror=\"alert('XSS')\">"},
                {"action": "CLICK", "selector": "#renderBtn"},
                {"action": "WAIT", "value": "600"},
            ],
            "expected_result": "The payload is sanitised and rendered as plain text without "
                               "executing any script.",
        },
        {
            "scenario_name": "Total is auto-calculated and locked",
            "type": "positive",
            "_expect": "PASS",
            "_proves": "VERIFY_DISABLED + VERIFY_NON_ZERO on a formula field",
            "steps": [
                {"action": "TYPE", "selector": "#qty", "value": "6"},
                {"action": "VERIFY_DISABLED", "selector": "#total"},
                {"action": "VERIFY_NON_ZERO", "selector": "#total"},
                {"action": "VERIFY_VALUE", "selector": "#total", "value": "12"},
            ],
            "expected_result": "The total field is read-only and shows the calculated value.",
        },
        {
            "scenario_name": "Missing element produces a readable failure",
            "type": "positive",
            "_expect": "FAIL",
            "_proves": "retry + heal exhausted, then a human-readable error",
            "steps": [
                {"action": "CLICK", "selector": "#doesNotExistAnywhere"},
            ],
            "expected_result": "The button is clicked and the record is saved successfully.",
        },
    ]


async def main() -> int:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    httpd = serve_app(port)
    print(f"Self-test app served at {base_url}")
    print(f"Throwaway workspace: {WORKSPACE}\n")

    flow_path = config.flow_dir(PROJECT, FLOW)
    flow_path.mkdir(parents=True, exist_ok=True)
    suite = build_suite(base_url)

    (flow_path / "flow.json").write_text(json.dumps({
        "flow_name": FLOW, "target_url": f"{base_url}/index.html",
        "timestamp": "2026-07-31 00:00:00", "duration_seconds": 0,
        "step_count": len(suite),
    }, indent=4), encoding="utf-8")
    (flow_path / "test_cases.json").write_text(json.dumps(suite, indent=4), encoding="utf-8")
    (flow_path / "actions.json").write_text(json.dumps([
        {"action": "TYPE", "selector": "#name", "value": "Seeded Widget", "tag": "input"},
        {"action": "TYPE", "selector": "#code", "value": "SEED1", "tag": "input"},
        {"action": "CLICK", "selector": "#saveBtn", "tag": "button"},
    ], indent=4), encoding="utf-8")

    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)
        kind = event.get("event")
        if kind in ("start", "info", "error", "cancelled"):
            print(f"  · {event.get('message')}")
        elif kind == "test_start":
            print(f"▶ start #{event.get('index')} {event.get('name')}")
        elif kind == "test_end":
            # Include the index: with parallel workers the events interleave.
            print(f"⇒ end   #{event.get('index')} {event.get('status')}: "
                  f"{(event.get('verdict') or {}).get('reason', '')}")

    cfg = executor.RunConfig(
        project_name=PROJECT, flow_name=FLOW, mode="headless", workers=2,
        verdict_mode="rules", step_shots="fail", triggered_by="selftest",
    )

    try:
        report = await executor.execute_suite(cfg, emit)
    finally:
        httpd.shutdown()

    # ── Compare actual verdicts against what each scenario should produce ──
    expected_by_name = {c["scenario_name"]: c for c in suite}
    rows, failures = [], []
    for case in report["checkpoints"]:
        spec = expected_by_name.get(case["name"], {})
        want = spec.get("_expect", "?")
        got = case["status"]
        ok = want == got
        if not ok:
            failures.append((case["name"], want, got,
                             (case.get("verdict") or {}).get("reason", "")))
        rows.append((ok, case["index"], case["name"], want, got,
                     spec.get("_proves", ""),
                     (case.get("verdict") or {}).get("reason", "")))

    print("\n" + "=" * 118)
    print(f"{'':2} {'#':>3} {'SCENARIO':44} {'WANT':11} {'GOT':11} PROVES")
    print("-" * 118)
    for ok, index, name, want, got, proves, _reason in sorted(rows, key=lambda r: r[1]):
        print(f"{'OK' if ok else 'XX':2} {index:>3} {name[:44]:44} {want:11} {got:11} "
              f"{proves[:40]}")
    print("=" * 118)

    summary = (f"total={report['total_test_cases']} passed={report['passed_count']} "
               f"failed={report['failed_count']} "
               f"unverified={report['unverified_count']} "
               f"crashed={report['crash_count']} "
               f"duration={report['duration_seconds']}s")
    print(summary)

    # Artefacts that must exist after any run.
    artefacts = {
        "report.json": (flow_path / "report.json").exists(),
        "junit.xml": (flow_path / "junit.xml").exists(),
        "run_history.json": (flow_path / "run_history.json").exists(),
        "bugs.json": (config.project_dir(PROJECT) / "bugs.json").exists(),
        "screenshots": any((flow_path / "screenshots").glob("*.png")),
        "videos": any((flow_path / "videos").glob("*.webm")),
        "downloads": any((flow_path / "downloads").glob("*")),
    }
    print("\nArtefacts: " + ", ".join(
        f"{name}={'yes' if present else 'NO'}" for name, present in artefacts.items()))

    missing_artefacts = [name for name, present in artefacts.items() if not present]

    if failures:
        print("\nVERDICT MISMATCHES:")
        for name, want, got, reason in failures:
            print(f"  - {name}: wanted {want}, got {got} ({reason})")
    if missing_artefacts:
        print(f"\nMISSING ARTEFACTS: {missing_artefacts}")

    ok = not failures and not missing_artefacts
    print("\n" + ("SELF-TEST PASSED ✅" if ok else "SELF-TEST FAILED ❌"))
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
