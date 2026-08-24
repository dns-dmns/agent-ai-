"""Run a flow through the standard @playwright/test framework, from Run & Verify.

The platform has two execution worlds. Its own engine (``executor.py``) collects
evidence and produces reasoned PASS/FAIL/UNVERIFIED/CRASH verdicts. The exported
POM framework runs on stock ``@playwright/test``, where a test passes when no
assertion threw. This module lets the Run & Verify section execute the second
world on demand — the same files the export produces, generated into a scratch
workspace and driven by ``npx playwright test``.

Honesty constraints, stated once and enforced here:

* Results from this engine are **assertions only**. There is no UNVERIFIED, no
  message scanning, no rules — a scenario with no assertion passes by not
  crashing. Every report this module writes is labelled ``engine: "playwright"``
  so it can never be mistaken for a verdict-engine report.
* Nothing is written into the user's project except one clearly-named report
  file. The framework itself lives under the platform's scratch directory
  (``.tmp/pw_runner/<project>/``), where ``node_modules`` survives between runs
  so only the first run pays the install.
* Credentials go to the child process as environment variables — never written
  into the generated files, which keep the blank ``credentials.json`` the export
  ships with.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import config

log = config.get_logger("framework_runner")

#: Wall-clock cap for the npx test process. A hung server must not pin the
#: platform forever; a legitimate long suite on a slow app stays well inside.
RUN_TIMEOUT_SECONDS = int(os.getenv("QA_FRAMEWORK_RUN_TIMEOUT", "1800"))

_STATUS_MAP = {
    # @playwright/test spec outcomes → the platform's vocabulary. "flaky"
    # means it failed and then passed on retry — reported as PASS but the
    # flakiness is preserved in the note, not laundered away.
    "expected": "PASS",
    "unexpected": "FAIL",
    "flaky": "PASS",
    "skipped": "SKIPPED",
}


def runner_dir(project_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name) or "project"
    return config.TEMP_DIR / "pw_runner" / safe


def node_available() -> str:
    """Path to npx, or '' — checked before promising anything to the stream."""
    return shutil.which("npx") or ""


def write_framework(project_name: str, files: dict[str, str]) -> Path:
    """Materialise the generated framework, preserving node_modules.

    Generated folders are wiped first so a renamed flow does not leave a stale
    spec behind to run alongside the fresh ones.
    """
    root = runner_dir(project_name)
    for sub in ("pages", "tests", "test-data", "fixtures", "utils"):
        shutil.rmtree(root / sub, ignore_errors=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def parse_results(raw: dict) -> list[dict]:
    """Flatten the @playwright/test JSON reporter into one row per test.

    The reporter nests suites arbitrarily deep (file → describe → …), so this
    walks rather than indexes. Unknown outcome strings map to FAIL, not PASS —
    a new Playwright status must never silently count as a pass.
    """
    rows: list[dict] = []

    def error_text(test: dict) -> str:
        for result in test.get("results") or []:
            err = result.get("error") or {}
            message = str(err.get("message") or "").strip()
            if message:
                # First line only: the full stack is in the raw file.
                return message.splitlines()[0][:300]
        return ""

    def walk(suite: dict) -> None:
        for spec in suite.get("specs") or []:
            for test in spec.get("tests") or []:
                outcome = str(test.get("status") or "unexpected")
                duration = sum(int(r.get("duration") or 0)
                               for r in test.get("results") or [])
                rows.append({
                    "name": str(spec.get("title") or "unnamed test"),
                    "file": str(spec.get("file") or ""),
                    "status": _STATUS_MAP.get(outcome, "FAIL"),
                    "outcome": outcome,
                    "duration_ms": duration,
                    "error": error_text(test),
                })
        for child in suite.get("suites") or []:
            walk(child)

    for suite in raw.get("suites") or []:
        walk(suite)
    return rows


def build_report(project_name: str, flow_name: str, rows: list[dict],
                 duration_seconds: int, run_id: str) -> dict:
    return {
        # The label that keeps this honest: assertion results, not verdicts.
        "engine": "playwright",
        "engine_note": ("Standard @playwright/test run: a test passes when no "
                        "assertion throws. No UNVERIFIED, no message scanning, "
                        "no rules — use the platform engine for verdicts."),
        "run_id": run_id,
        "project_name": project_name,
        "flow_name": flow_name,
        "execution_date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": duration_seconds,
        "total": len(rows),
        "passed_count": sum(1 for r in rows if r["status"] == "PASS"),
        "failed_count": sum(1 for r in rows if r["status"] == "FAIL"),
        "skipped_count": sum(1 for r in rows if r["status"] == "SKIPPED"),
        "flaky_count": sum(1 for r in rows if r["outcome"] == "flaky"),
        "tests": rows,
    }


def persist_report(report: dict) -> Path:
    """One clearly-named file, kept apart from the verdict engine's report.json.

    Deliberately NOT written into report.json or run_history.json: those hold
    verdict-engine results, and flaky-detection and trend queries read them.
    Mixing assertion-only rows in would quietly change what those numbers mean.
    """
    project = report["project_name"]
    flow = report.get("flow_name") or ""
    if flow:
        base = config.flow_dir(project, flow)
    else:
        base = config.project_dir(project)
    base.mkdir(parents=True, exist_ok=True)
    path = base / "report.playwright.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


async def _stream_process(command: list[str], cwd: Path, env: dict,
                          emit, state, label: str) -> int:
    """Run one subprocess, forwarding each output line; honours Stop."""
    process = await asyncio.create_subprocess_exec(
        *command, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    started = time.time()
    assert process.stdout is not None
    while True:
        if state is not None and state.cancelled:
            process.kill()
            await emit(f"⏹ {label} stopped by request.")
            return -1
        if time.time() - started > RUN_TIMEOUT_SECONDS:
            process.kill()
            await emit(f"⏱ {label} exceeded {RUN_TIMEOUT_SECONDS}s and was stopped.")
            return -2
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        if text:
            await emit(text)
    return await process.wait()


async def run_framework(project_name: str, flow_name: str, files: dict[str, str],
                        spec_file: str, credentials: dict, emit, state) -> dict | None:
    """Generate → install (first run only) → execute → parse → persist.

    ``emit`` receives human-readable lines for the terminal; the parsed report
    is returned (and persisted), or ``None`` when the run never reached the
    test stage.
    """
    npx = node_available()
    if not npx:
        await emit("❌ Node.js was not found on this machine. The framework "
                   "engine runs the exported @playwright/test project, which "
                   "needs Node 18+ — install it from https://nodejs.org, or "
                   "use the Platform engine which needs no Node at all.")
        return None

    root = write_framework(project_name, files)
    await emit(f"📁 Framework generated at {root} "
               f"({len(files)} files; node_modules is reused between runs).")

    env = dict(os.environ)
    env["TEST_USERNAME"] = str(credentials.get("username") or "")
    env["TEST_PASSWORD"] = str(credentials.get("password") or "")
    results_file = root / "results.json"
    env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(results_file)

    if not (root / "node_modules" / "@playwright" / "test").exists():
        npm = shutil.which("npm")
        if not npm:
            await emit("❌ npm was not found next to Node — reinstall Node.js "
                       "from https://nodejs.org.")
            return None
        await emit("📦 First run for this project — installing @playwright/test "
                   "(one time; later runs skip this)…")
        code = await _stream_process([npm, "install"],
                                     root, env, emit, state, "npm install")
        if code != 0:
            await emit(f"❌ npm install failed (exit {code}). The framework "
                       f"folder is {root} — run 'npm install' there to see why.")
            return None

    # A no-op when the browser is already cached; a download when it is not.
    await _stream_process([npx, "playwright", "install", "chromium"],
                          root, env, emit, state, "browser install")

    command = [npx, "playwright", "test"]
    if spec_file:
        command.append(spec_file)
    command += ["--reporter=list,json"]
    await emit(f"🎭 npx playwright test {spec_file or '(all specs)'} — "
               f"assertion results, not verdicts.")
    started = time.time()
    exit_code = await _stream_process(command, root, env, emit, state,
                                      "playwright test")

    if not results_file.exists():
        if exit_code not in (-1, -2):
            await emit("❌ The runner produced no results file — the process "
                       f"exited with {exit_code} before any test ran. The lines "
                       "above are the actual cause.")
        return None

    try:
        raw = json.loads(results_file.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001 - a truncated file on kill is expected
        await emit(f"⚠️ Results file could not be parsed ({err}) — the run was "
                   "probably stopped mid-write.")
        return None

    rows = parse_results(raw)
    report = build_report(project_name, flow_name, rows,
                          int(time.time() - started),
                          getattr(state, "run_id", ""))
    path = persist_report(report)
    await emit(f"💾 Report saved to {path.relative_to(config.BASE_DIR)}")
    return report
