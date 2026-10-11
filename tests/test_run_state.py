"""Run state survives a restart, and a run that raises never stays 'running'."""
import asyncio
import os
import sys
import tempfile

os.environ["QA_WORKSPACE"] = os.environ.get("QA_WORKSPACE") or tempfile.mkdtemp(prefix="qa_runstate_")
os.environ.setdefault("QA_SCHEDULER", "0")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import pytest  # noqa: E402

from app import db, executor  # noqa: E402


@pytest.fixture(autouse=True)
def _schema():
    db.init()


def test_registered_run_is_durable_and_restart_marks_it_interrupted():
    state = executor.register_run("proj", "Flow")
    executor.RUN_REGISTRY.pop(state.run_id)          # what a restart does

    snap = db.load_run_state(state.run_id)
    assert snap["status"] == "running"

    assert db.mark_interrupted_runs() >= 1
    snap = db.load_run_state(state.run_id)
    assert snap["status"] == "interrupted"
    assert snap["exit_code"] == 1


def test_exit_code_is_zero_only_for_a_clean_complete_run():
    state = executor.RunState(run_id="x", project_name="p", flow_name="f")
    assert state.snapshot()["exit_code"] == 1        # still running
    state.status = "complete"
    assert state.snapshot()["exit_code"] == 0
    state.failed = 1
    assert state.snapshot()["exit_code"] == 1
    state.failed, state.status = 0, "cancelled"
    assert state.snapshot()["exit_code"] == 1


def test_a_run_that_raises_is_marked_error_not_left_running(monkeypatch):
    async def boom(cfg, emit, state):
        raise RuntimeError("browser died")

    monkeypatch.setattr(executor, "_execute_suite", boom)
    cfg = executor.RunConfig(project_name="proj", flow_name="Flow")
    state = executor.register_run("proj", "Flow")

    async def emit(_event):
        return None

    with pytest.raises(RuntimeError):
        asyncio.run(executor.execute_suite(cfg, emit, state))

    assert state.status == "error"
    assert "browser died" in state.error
    assert db.load_run_state(state.run_id)["status"] == "error"
