"""Backward-compatibility check against the real project data in this repo.

Boots the server on the actual workspace and verifies that everything recorded
before the upgrade still loads and still parses: old ``actions.json`` without
candidate selectors, old ``report.json`` without verdict/unverified fields, and
old ``test_cases.json`` whose steps use verbs the runner previously ignored.

Read-only: it never executes a suite, never writes to a flow, and never deletes
anything.

    .venv\\Scripts\\python.exe tests\\regression_existing_data.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import executor      # noqa: E402
from app import verdict as v  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(base: str, path: str, **params) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{base}{path}?{query}" if query else f"{base}{path}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read())


def main() -> int:
    port = free_port()
    # This test is the only one that boots against the REAL workspace, so the
    # scheduler is disabled: a test must never trigger a live suite run.
    env = dict(os.environ, QA_SCHEDULER="0", QA_LOG_LEVEL="WARNING")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "qa_agent:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    try:
        for _ in range(60):
            if server.poll() is not None:
                print("Server exited early:\n" + (server.stdout.read() if server.stdout else ""))
                return 1
            try:
                health = get(base, "/health")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("Server never became healthy.")
            return 1

        print(f"Workspace under test: {health['workspace']}")
        record("server healthy", health.get("status") == "ok")

        projects = get(base, "/list-projects")["projects"]
        print(f"Projects: {projects}")
        # Data-driven on purpose: this checks whatever is in the workspace today,
        # so it keeps working when flows are re-recorded or replaced.
        record("at least one project is listed", bool(projects), str(projects))

        total_flows = total_cases = 0
        step_actions: Counter = Counter()
        unsupported: Counter = Counter()
        expectation_kinds = Counter()
        old_reports = 0
        reported_flows: list[tuple[str, str]] = []

        supported = executor.ASSERTION_ACTIONS | {
            "NAVIGATE", "CLICK", "DOUBLE_CLICK", "RIGHT_CLICK", "TYPE", "CLEAR", "SELECT",
            "SELECT_OPTION", "CHOOSE", "CHECK", "UNCHECK", "KEYPRESS", "PRESS", "KEY",
            "UPLOAD", "ATTACH", "HOVER", "SCROLL", "SCROLL_TO", "WAIT", "SLEEP",
            "WAIT_FOR", "WAIT_FOR_ELEMENT", "DIALOG", "SCREENSHOT", "CAPTURE",
        }

        for project in projects:
            flows = get(base, "/list-flows", project_name=project)
            for flow in flows["auth_flows"] + flows["business_flows"]:
                total_flows += 1
                details = get(base, "/get-flow-details", project_name=project, flow_name=flow)
                record(f"{project}/{flow}: details load", isinstance(details.get("flow"), dict))

                for case in details.get("test_cases", []) or []:
                    total_cases += 1
                    for step in executor.parse_steps(case.get("steps", [])):
                        action = (step.get("action") or "").upper()
                        step_actions[action] += 1
                        if action not in supported:
                            unsupported[action] += 1
                    intent = v.classify_expectation(case.get("expected_result", ""))
                    if intent["expects_error"]:
                        expectation_kinds["negative"] += 1
                    elif intent["expects_success"]:
                        expectation_kinds["positive"] += 1
                    else:
                        expectation_kinds["unclassified"] += 1

                report = details.get("latest_report")
                if report:
                    reported_flows.append((project, flow))
                    # Pre-upgrade reports have no unverified_count / verdict keys.
                    if "unverified_count" not in report:
                        old_reports += 1
                    record(f"{project}/{flow}: old report readable",
                           isinstance(report.get("checkpoints"), list))

        print(f"\nFlows: {total_flows} · test cases: {total_cases} · legacy reports: {old_reports}")
        print(f"Step verbs found: {dict(step_actions)}")
        print(f"Expectation classification: {dict(expectation_kinds)}")

        record("every recorded step verb is executable", not unsupported, str(dict(unsupported)))
        if total_cases:
            record("expectations are classifiable",
                   expectation_kinds["unclassified"] < max(1, total_cases * 0.3),
                   f"{expectation_kinds['unclassified']} of {total_cases} unclassified")
        else:
            print("No test cases in the workspace yet — verb and expectation checks skipped.")

        for project in projects:
            bugs = get(base, "/get-project-bugs", project_name=project)
            record(f"{project}: bugs.json readable", isinstance(bugs.get("bugs"), list))

        # Reports and history are checked against whichever flow actually has
        # them, so re-recording or replacing flows never breaks this test.
        if reported_flows:
            project, flow = reported_flows[0]
            history = get(base, "/get-run-history", project_name=project, flow_name=flow)
            record("run history readable", isinstance(history.get("history"), list))

            report = get(base, "/get-report", project_name=project, flow_name=flow)
            passed = sum(1 for c in report.get("checkpoints", []) if c.get("status") == "PASS")
            unverified = sum(1 for c in report.get("checkpoints", []) if not c.get("verdict"))
            print(f"\nReport for '{project}/{flow}': {passed} PASS, {unverified} of them "
                  f"carrying no verdict information (pre-upgrade runs).")
        else:
            print("\nNo saved reports in this workspace — report checks skipped.")

        print("\n" + "=" * 92)
        failures = [c for c in checks if not c[1]]
        for name, ok, detail in checks:
            if not ok:
                print(f"XX  {name}  → {detail}")
        print(f"{len(checks) - len(failures)}/{len(checks)} backward-compatibility checks passed")
        print("=" * 92)
        print("REGRESSION CHECK PASSED ✅" if not failures else "REGRESSION CHECK FAILED ❌")
        return 0 if not failures else 1
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


if __name__ == "__main__":
    sys.exit(main())
