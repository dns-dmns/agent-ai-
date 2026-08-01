"""Integration test for the automation-facing API surface.

Covers the paths a human never clicks but CI depends on:

* ``POST /trigger-run`` → ``GET /run-status/{id}`` → ``GET /export-junit``
* ``POST /stop-run`` actually cancels a running suite
* ``QA_API_KEY`` rejects unauthenticated calls and accepts authenticated ones
* flow maintenance (duplicate / rename / delete) and the soft-delete trash
* test-case editing endpoints

Runs the platform against the bundled sample app in a throwaway workspace.

    .venv\\Scripts\\python.exe tests\\integration_api.py
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP_DIR = HERE / "selftest_app"

WORKSPACE = Path(tempfile.mkdtemp(prefix="qa_api_"))
PROJECT = "ApiProject"
FLOW = "widget master"
API_KEY = "unit-test-key-123"

checks: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(("OK  " if ok else "XX  ") + name + (f"  → {detail}" if not ok and detail else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve_app(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(APP_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def call(base: str, path: str, *, method: str = "GET", key: str | None = API_KEY,
         raw: bool = False, **params):
    query = urllib.parse.urlencode(params)
    url = f"{base}{path}?{query}" if query else f"{base}{path}"
    request = urllib.request.Request(url, method=method)
    if key:
        request.add_header("X-API-Key", key)
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
        return body.decode("utf-8", "replace") if raw else json.loads(body)


def post_json(base: str, path: str, body: dict, key: str | None = API_KEY, **params) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{base}{path}?{query}" if query else f"{base}{path}"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def seed(app_url: str) -> None:
    flow_dir = WORKSPACE / "projects" / PROJECT / "flows" / FLOW
    flow_dir.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "web").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "web" / "dashboard.html", WORKSPACE / "web" / "dashboard.html")

    (flow_dir / "flow.json").write_text(json.dumps({
        "flow_name": FLOW, "target_url": f"{app_url}/index.html",
        "timestamp": "2026-07-31 09:00:00", "duration_seconds": 5, "step_count": 2,
    }), encoding="utf-8")
    (flow_dir / "actions.json").write_text(json.dumps([
        {"action": "TYPE", "selector": "#name", "value": "Seed", "tag": "input"},
    ]), encoding="utf-8")
    (flow_dir / "test_cases.json").write_text(json.dumps([
        {"scenario_name": "Create widget", "type": "positive",
         "steps": [{"action": "TYPE", "selector": "#name", "value": "CI Widget"},
                   {"action": "TYPE", "selector": "#code", "value": "CI001"},
                   {"action": "CLICK", "selector": "#saveBtn"},
                   {"action": "VERIFY_MESSAGE", "value": "saved successfully"}],
         "expected_result": "Widget is created successfully."},
        {"scenario_name": "Empty name rejected", "type": "negative",
         "steps": [{"action": "CLICK", "selector": "#saveBtn"},
                   {"action": "VERIFY_MESSAGE", "value": "Name is required"}],
         "expected_result": "Validation error appears indicating the name is required."},
        {"scenario_name": "Slow scenario for cancellation", "type": "positive",
         "steps": [{"action": "WAIT", "value": "3000"},
                   {"action": "WAIT", "value": "3000"},
                   {"action": "WAIT", "value": "3000"},
                   {"action": "WAIT", "value": "3000"}],
         "expected_result": "The page stays responsive."},
    ], indent=2), encoding="utf-8")


def main() -> int:
    app_port, api_port = free_port(), free_port()
    httpd = serve_app(app_port)
    app_url = f"http://127.0.0.1:{app_port}"
    seed(app_url)

    env = dict(os.environ, QA_WORKSPACE=str(WORKSPACE), QA_SCHEDULER="0",
               QA_API_KEY=API_KEY, QA_LOG_LEVEL="WARNING")
    env.pop("GEMINI_API_KEY", None)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "qa_agent:app", "--host", "127.0.0.1",
         "--port", str(api_port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{api_port}"
    print(f"Platform: {base}  ·  sample app: {app_url}\nWorkspace: {WORKSPACE}\n")

    try:
        for _ in range(60):
            if server.poll() is not None:
                print("Server exited early:\n" + (server.stdout.read() if server.stdout else ""))
                return 1
            try:
                health = call(base, "/health", key=None)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("Server never became healthy.")
            return 1

        record("health is public even with auth on", health.get("status") == "ok")
        record("auth reported enabled", health.get("auth_enabled") is True)

        # ── API key enforcement ───────────────────────────────────────
        try:
            call(base, "/list-projects", key=None)
            record("unauthenticated call rejected", False, "request succeeded without a key")
        except urllib.error.HTTPError as err:
            record("unauthenticated call rejected", err.code == 401, f"HTTP {err.code}")

        try:
            call(base, "/list-projects", key="wrong-key")
            record("wrong key rejected", False, "request succeeded with a wrong key")
        except urllib.error.HTTPError as err:
            record("wrong key rejected", err.code == 401, f"HTTP {err.code}")

        projects = call(base, "/list-projects")
        record("authenticated call accepted", PROJECT in projects["projects"])

        # ── Test-case editing ─────────────────────────────────────────
        added = post_json(base, "/add-test-case", {
            "scenario_name": "Integration added", "type": "positive",
            "steps": [{"action": "CLICK", "selector": "#saveBtn"}],
            "expected_result": "Nothing in particular.",
        }, project_name=PROJECT, flow_name=FLOW)
        record("add-test-case", added.get("status") == "success", str(added))
        deleted = call(base, "/delete-test-case", method="POST", project_name=PROJECT,
                       flow_name=FLOW, case_index=added["index"])
        record("delete-test-case", deleted.get("status") == "success", str(deleted))

        # ── CI/CD: trigger → poll → JUnit ─────────────────────────────
        triggered = call(base, "/trigger-run", method="POST", project_name=PROJECT,
                         flow_name=FLOW, mode="headless", workers=2, case_ids="1,2",
                         verdict_mode="rules", triggered_by="integration")
        run_id = triggered.get("run_id")
        record("trigger-run returns a run id", bool(run_id), str(triggered))

        status = {}
        for _ in range(120):
            status = call(base, f"/run-status/{run_id}")
            if status.get("status") != "running":
                break
            time.sleep(1)
        record("run completes", status.get("status") == "complete", json.dumps(status))
        record("both scenarios passed", status.get("passed") == 2, json.dumps(status))
        record("exit_code is 0 on success", status.get("exit_code") == 0, str(status.get("exit_code")))

        junit = call(base, "/export-junit", raw=True, project_name=PROJECT, flow_name=FLOW)
        record("JUnit export well formed",
               "<testsuites" in junit and 'tests="2"' in junit, junit[:160])

        csv_body = call(base, "/export-report-csv", raw=True, project_name=PROJECT,
                        flow_name=FLOW)
        record("CSV export has a row per scenario",
               csv_body.count("\n") >= 3 and "Verdict reason" in csv_body)

        history = call(base, "/get-run-history", project_name=PROJECT, flow_name=FLOW)
        record("run history recorded", len(history.get("history", [])) >= 1)

        runs = call(base, "/list-runs", project_name=PROJECT)
        record("list-runs includes the DB history", isinstance(runs.get("history"), list))

        # ── Cancellation ──────────────────────────────────────────────
        slow = call(base, "/trigger-run", method="POST", project_name=PROJECT,
                    flow_name=FLOW, mode="headless", workers=1, case_ids="3",
                    verdict_mode="rules", triggered_by="integration-cancel")
        slow_id = slow["run_id"]
        time.sleep(4)
        stopped = call(base, "/stop-run", method="POST", run_id=slow_id)
        record("stop-run accepted", stopped.get("status") == "success", str(stopped))

        final = {}
        for _ in range(60):
            final = call(base, f"/run-status/{slow_id}")
            if final.get("status") != "running":
                break
            time.sleep(1)
        record("run reports cancelled", final.get("status") == "cancelled", json.dumps(final))

        # ── Flow maintenance & soft delete ────────────────────────────
        dup = call(base, "/duplicate-flow", method="POST", project_name=PROJECT,
                   flow_name=FLOW, new_name="widget copy")
        record("duplicate-flow", dup.get("status") == "success", str(dup))
        renamed = call(base, "/rename-flow", method="POST", project_name=PROJECT,
                       flow_name="widget copy", new_name="widget renamed")
        record("rename-flow", renamed.get("status") == "success", str(renamed))
        flows = call(base, "/list-flows", project_name=PROJECT)
        record("renamed flow listed", "widget renamed" in flows["business_flows"],
               str(flows))

        removed = call(base, "/delete-flow", method="POST", project_name=PROJECT,
                       flow_name="widget renamed")
        record("delete-flow", removed.get("status") == "success", str(removed))
        record("soft delete kept a trash copy",
               any((WORKSPACE / ".trash").glob("*widget renamed*")),
               str(list((WORKSPACE / ".trash").glob("*")) if (WORKSPACE / ".trash").exists() else []))

        # ── Destructive-parameter guards ──────────────────────────────
        # An empty name used to resolve to the projects root, which would have
        # sent the WHOLE workspace to the trash.
        for label, path, params in (
            ("empty project_name on delete-project", "/delete-project", {"project_name": ""}),
            ("empty flow_name on delete-flow", "/delete-flow",
             {"project_name": PROJECT, "flow_name": ""}),
            ("empty flow_name on rename-flow", "/rename-flow",
             {"project_name": PROJECT, "flow_name": "", "new_name": "x"}),
            ("empty new_name on rename-flow", "/rename-flow",
             {"project_name": PROJECT, "flow_name": FLOW, "new_name": ""}),
            ("dot-prefixed project_name", "/delete-project", {"project_name": ".trash"}),
        ):
            try:
                call(base, path, method="POST", **params)
                record(f"blocked: {label}", False, "request was accepted!")
            except urllib.error.HTTPError as err:
                record(f"blocked: {label}", err.code in (400, 404), f"HTTP {err.code}")

        record("workspace survived the guard probes",
               (WORKSPACE / "projects" / PROJECT).is_dir() and
               any((WORKSPACE / "projects" / PROJECT / "flows").iterdir()))

        # ── Path traversal guard ──────────────────────────────────────
        try:
            call(base, "/get-flow-details", project_name="../..", flow_name="x")
            record("path traversal blocked", False, "request was accepted")
        except urllib.error.HTTPError as err:
            record("path traversal blocked", err.code == 400, f"HTTP {err.code}")

        # ── Project settings round trip ────────────────────────────────
        saved = post_json(base, "/save-project-config", {
            "environments": {"dev": {"base_url": app_url}},
            "active_env": "dev",
            "credentials": {"username": "admin", "password": "secret"},
            "notifications": {"webhook_url": "", "on": "failures_only"},
        }, project_name=PROJECT)
        record("project settings saved", saved.get("status") == "success", str(saved))

        loaded = call(base, "/get-project-config", project_name=PROJECT)
        record("environment persisted", loaded["environments"]["dev"]["base_url"] == app_url)
        record("password never echoed back",
               loaded["credentials"]["password"] == "********",
               str(loaded["credentials"]))

        failures = [c for c in checks if not c[1]]
        print("\n" + "=" * 92)
        print(f"{len(checks) - len(failures)}/{len(checks)} API integration checks passed")
        print("=" * 92)
        print("API INTEGRATION PASSED ✅" if not failures else "API INTEGRATION FAILED ❌")
        return 0 if not failures else 1
    finally:
        httpd.shutdown()
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
    sys.exit(main())
