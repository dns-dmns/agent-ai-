"""Unit tests for the platform modules: API seeding, history DB, schedules.

These run against a temporary workspace so they never touch real project data.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Redirect the workspace before config is imported by anything else.
_TMP = Path(tempfile.mkdtemp(prefix="qa_unit_"))
os.environ["QA_WORKSPACE"] = str(_TMP)
os.environ["QA_DB_PATH"] = str(_TMP / "test.db")
os.environ["QA_SCHEDULER"] = "0"

from app import config  # noqa: E402

importlib.reload(config)

from app import api_seed   # noqa: E402
from app import db         # noqa: E402
from app import scheduler  # noqa: E402

importlib.reload(api_seed)
importlib.reload(db)
importlib.reload(scheduler)

PROJECT = "UnitProject"


# ──────────────────────────────────────────────────────────────────────
# API pre-seeding (Pillar 3)
# ──────────────────────────────────────────────────────────────────────
def test_extract_supports_nested_paths_and_indexes():
    payload = {"data": {"items": [{"id": 42, "name": "Cotton"}]}}
    assert api_seed._extract(payload, "data.items.0.id") == 42
    assert api_seed._extract(payload, "data.items.0.name") == "Cotton"
    assert api_seed._extract(payload, "data.missing") is None
    assert api_seed._extract(payload, "data.items.9.id") is None


def test_seed_definitions_round_trip():
    definition = {
        "base_url": "https://erp.example.test",
        "requests": [{"name": "category", "method": "POST", "path": "/api/category",
                      "json": {"name": "CAT_{{run_token}}"},
                      "capture": {"category_id": "data.id"}}],
    }
    api_seed.save_seed(PROJECT, "masters", definition)
    loaded = api_seed.load_seeds(PROJECT)
    assert "masters" in loaded["seeds"]
    assert loaded["seeds"]["masters"]["requests"][0]["path"] == "/api/category"

    assert api_seed.delete_seed(PROJECT, "masters") is True
    assert api_seed.delete_seed(PROJECT, "masters") is False


def test_seed_payload_resolves_variables():
    resolved = api_seed._resolve(
        {"name": "CAT_{{run_token}}", "nested": {"code": "{{category_id}}"},
         "list": ["{{run_token}}"], "untouched": 7},
        {"run_token": "ABC", "category_id": 99},
    )
    assert resolved["name"] == "CAT_ABC"
    assert resolved["nested"]["code"] == "99"
    assert resolved["list"] == ["ABC"]
    assert resolved["untouched"] == 7


# ──────────────────────────────────────────────────────────────────────
# History database
# ──────────────────────────────────────────────────────────────────────
def _report(run_id: str, statuses: list[str]) -> dict:
    return {
        "run_id": run_id, "project_name": PROJECT, "flow_name": "orders",
        "execution_date": "2026-07-31 10:00:00", "duration_seconds": 10,
        "mode": "headless", "workers": 2, "verdict_mode": "rules",
        "environment": "", "triggered_by": "unit",
        "total_test_cases": len(statuses),
        "passed_count": statuses.count("PASS"),
        "failed_count": statuses.count("FAIL"),
        "unverified_count": statuses.count("UNVERIFIED"),
        "crash_count": statuses.count("CRASH"),
        "checkpoints": [
            {"index": i + 1, "name": f"scenario {i + 1}", "type": "positive",
             "status": status, "duration": 2, "failed_step": 0,
             "verdict": {"reason": f"because {status}", "source": "rules"}}
            for i, status in enumerate(statuses)
        ],
    }


def test_record_run_and_query_history():
    db.init()
    db.record_run(_report("unit-run-1", ["PASS", "FAIL"]))
    runs = db.recent_runs(PROJECT, "orders")
    assert any(r["run_id"] == "unit-run-1" for r in runs)

    history = db.scenario_history(PROJECT, "orders", "scenario 2")
    assert history and history[0]["status"] == "FAIL"

    stats = db.stats(PROJECT)
    assert stats["runs"] >= 1
    assert stats["by_status"].get("PASS", 0) >= 1


def test_flaky_detection_spots_oscillating_scenario():
    db.init()
    # scenario 1 flips PASS → FAIL → PASS; scenario 2 is stable.
    db.record_run(_report("flaky-a", ["PASS", "PASS"]))
    db.record_run(_report("flaky-b", ["FAIL", "PASS"]))
    db.record_run(_report("flaky-c", ["PASS", "PASS"]))

    flaky = db.flaky_scenarios(PROJECT)
    names = {item["scenario_name"] for item in flaky}
    assert "scenario 1" in names
    assert "scenario 2" not in names


def test_audit_log_is_written():
    db.audit("unit_action", "target", details={"k": "v"})
    with db.connect() as conn:
        row = conn.execute(
            "SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["action"] == "unit_action"


# ──────────────────────────────────────────────────────────────────────
# Schedules
# ──────────────────────────────────────────────────────────────────────
def test_schedule_round_trip_and_validation():
    saved = scheduler.save_schedule(PROJECT, {
        "name": "Nightly", "flow_name": "orders",
        "trigger": {"type": "daily", "at": "02:00"},
        "config": {"mode": "headless", "workers": 2},
    })
    assert saved["id"]
    assert scheduler.load_schedules(PROJECT)[0]["name"] == "Nightly"

    # Updating by id replaces rather than duplicates.
    scheduler.save_schedule(PROJECT, dict(saved, name="Nightly v2"))
    schedules = scheduler.load_schedules(PROJECT)
    assert len(schedules) == 1
    assert schedules[0]["name"] == "Nightly v2"

    assert scheduler.delete_schedule(PROJECT, saved["id"]) is True
    assert scheduler.load_schedules(PROJECT) == []


def test_schedule_validation_rejects_bad_input():
    import pytest

    with pytest.raises(ValueError):
        scheduler.save_schedule(PROJECT, {"trigger": {"type": "daily", "at": "02:00"}})
    with pytest.raises(ValueError):
        scheduler.save_schedule(PROJECT, {"flow_name": "orders",
                                          "trigger": {"type": "hourly"}})
    with pytest.raises(ValueError):
        scheduler.save_schedule(PROJECT, {"flow_name": "orders",
                                          "trigger": {"type": "interval"}})


# ──────────────────────────────────────────────────────────────────────
# Workspace configuration
# ──────────────────────────────────────────────────────────────────────
def test_workspace_paths_follow_the_env_variable():
    assert config.BASE_DIR == _TMP
    assert config.PROJECTS_DIR == _TMP / "projects"
    assert config.flow_dir("P", "F") == _TMP / "projects" / "P" / "flows" / "F"
    assert config.static_url("P", "F", "screenshots", "a.png") == \
        "/static/projects/P/flows/F/screenshots/a.png"
