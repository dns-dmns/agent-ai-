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
def _report(run_id: str, statuses: list[str], flow_name: str = "orders") -> dict:
    return {
        "run_id": run_id, "project_name": PROJECT, "flow_name": flow_name,
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
    # Its own flow: flakiness is judged from run history, so rows recorded by
    # another test would otherwise change the verdict depending on test order.
    flow = "flaky-flow"
    # scenario 1 flips PASS → FAIL → PASS; scenario 2 is stable.
    db.record_run(_report("flaky-a", ["PASS", "PASS"], flow))
    db.record_run(_report("flaky-b", ["FAIL", "PASS"], flow))
    db.record_run(_report("flaky-c", ["PASS", "PASS"], flow))

    flaky = [f for f in db.flaky_scenarios(PROJECT) if f["flow_name"] == flow]
    names = {item["scenario_name"] for item in flaky}
    assert "scenario 1" in names, f"expected scenario 1 to be flaky, got {flaky}"
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


# ──────────────────────────────────────────────────────────────────────
# Rules
# ──────────────────────────────────────────────────────────────────────
from app import contracts  # noqa: E402
from app import rules      # noqa: E402

importlib.reload(contracts)
importlib.reload(rules)


class _Ev:
    """Minimal stand-in for verdict.Evidence."""

    def __init__(self, messages=None, downloads=None, console_errors=None,
                 url_start="", url_end=""):
        self.messages = [{"text": t, "selector": "", "kind": k}
                         for t, k in (messages or [])]
        self.downloads = downloads or []
        self.console_errors = console_errors or []
        self.assertions = []
        self.url_start = url_start
        self.url_end = url_end


def test_rule_only_matches_values_actually_entered_into_that_field():
    """Regression: matching rule values against the scenario as text accused a
    scenario that never touched the field — '0' appears inside 'timeout: 20000'."""
    rule = rules.validate({"title": "qty rejects zero", "kind": "field_rejects",
                           "selector": "#orderQty", "values": ["0"]})
    unrelated_steps = [
        {"action": "TYPE", "selector": "#orderQty", "value": "1"},
        {"action": "WAIT_FOR_STATE", "selector": "#status", "value": "Approved",
         "timeout": 20000},
    ]
    result = rules.evaluate([rule], _Ev(), {}, unrelated_steps)[0]
    assert result["skipped"] is True

    real_steps = [{"action": "TYPE", "selector": "#orderQty", "value": "0"}]
    violated = rules.evaluate([rule], _Ev(), {}, real_steps)[0]
    assert violated["skipped"] is False and violated["ok"] is False

    with_error = rules.evaluate(
        [rule], _Ev(messages=[("Quantity must be greater than 0", "error")]), {}, real_steps)[0]
    assert with_error["ok"] is True


def test_rule_kinds_evaluate_as_documented():
    nav = rules.validate({"title": "stay", "kind": "must_not_navigate",
                          "url_contains": "/fabric-category"})
    assert rules.evaluate([nav], _Ev(url_start="https://a/fabric-category",
                                     url_end="https://a/fabric-category"), {})[0]["ok"]
    assert not rules.evaluate([nav], _Ev(url_start="https://a/fabric-category",
                                         url_end="https://a/sulzer"), {})[0]["ok"]

    export = rules.validate({"title": "all rows", "kind": "export_complete", "min_rows": 100})
    assert not rules.evaluate([export], _Ev(downloads=[{"row_count": 20}]), {})[0]["ok"]
    assert rules.evaluate([export], _Ev(downloads=[{"row_count": 140}]), {})[0]["ok"]
    assert rules.evaluate([export], _Ev(), {})[0]["skipped"]

    console = rules.validate({"title": "clean", "kind": "no_console_errors"})
    assert rules.evaluate([console], _Ev(console_errors=["[warning] x"]), {})[0]["ok"]
    assert not rules.evaluate([console], _Ev(console_errors=["[error] boom"]), {})[0]["ok"]


def test_rules_round_trip_and_generate_scenarios():
    saved = rules.save(PROJECT, {"title": "Beam rejects zero", "kind": "field_rejects",
                                 "selector": "#beam", "values": ["0"],
                                 "generate_scenario": True}, "beam flow")
    assert rules.applicable(PROJECT, "beam flow")[0]["id"] == saved["id"]
    generated = rules.to_scenarios([saved])
    assert generated and generated[0]["_rule_id"] == saved["id"]
    assert rules.delete(PROJECT, saved["id"], "beam flow") is True


def test_invalid_rules_are_rejected_with_a_useful_message():
    import pytest
    with pytest.raises(ValueError, match="needs the field 'selector'"):
        rules.validate({"kind": "field_rejects", "values": ["0"]})
    with pytest.raises(ValueError, match="Unknown rule kind"):
        rules.validate({"kind": "teleport"})


# ──────────────────────────────────────────────────────────────────────
# Dependency contracts
# ──────────────────────────────────────────────────────────────────────
def test_contract_round_trip_and_capture_steps():
    contracts.save(PROJECT, "dept master", {
        "provides": [{"entity": "department", "var": "dept_name",
                      "name_field": "#deptName", "status_field": "#status",
                      "list_url": "/masters/dept"}],
        "run_once": True,
    })
    loaded = contracts.load(PROJECT, "dept master")
    assert loaded["provides"][0]["var"] == "dept_name"

    steps = contracts.capture_steps(loaded)
    assert steps[0]["action"] == "STORE" and steps[0]["value"] == "dept_name"

    providers = contracts.provider_map(PROJECT, "dept master")
    assert providers["department"]["flow"] == "dept master"


def test_contract_validation_demands_a_capture_field():
    import pytest
    with pytest.raises(ValueError, match="name_field"):
        contracts.validate({"provides": [{"entity": "x", "var": "y"}]})

    # Two providers writing the same variable would silently shadow each other.
    with pytest.raises(ValueError, match="both capture"):
        contracts.validate({"provides": [
            {"entity": "a", "var": "same", "name_field": "#a"},
            {"entity": "b", "var": "same", "name_field": "#b"}]})

    # An unknown consumer kind would produce a check that can never run.
    with pytest.raises(ValueError, match="kind"):
        contracts.validate({
            "requires": [{"entity": "a", "selector": "#x", "kind": "telepathy"}]})


def test_relationship_checks_cover_the_four_cases_and_explain_gaps():
    contracts.save(PROJECT, "employee", {
        "requires": [{"entity": "department", "selector": "#deptDropdown"}],
    })
    scenarios, notes = contracts.relationship_checks(PROJECT, "employee", "dept master")
    names = " ".join(s["scenario_name"] for s in scenarios)
    assert "Active department appears" in names
    assert "Inactive department is excluded" in names
    assert "cannot be deleted" in names
    assert not notes

    # Without a provider the gap is explained rather than silently skipped.
    _, missing_notes = contracts.relationship_checks(PROJECT, "employee", "")
    assert missing_notes and "No chained prerequisite provides" in missing_notes[0]


def test_unresolved_requirements_are_reported():
    unresolved = contracts.unresolved_requirements(PROJECT, "employee", "")
    assert unresolved and unresolved[0]["entity"] == "department"
    assert contracts.unresolved_requirements(PROJECT, "employee", "dept master") == []


def test_rules_can_be_scoped_to_scenario_types():
    """'A failed login must stay on the login page' is true for negative cases
    and false for the positive one — without scoping it would fail a good test."""
    rule = rules.validate({
        "title": "failed login stays put", "kind": "must_not_navigate",
        "url_contains": "/auth/login", "types": ["negative", "security"],
    })
    positive = rules.evaluate([rule], _Ev(url_start="https://x/auth/login",
                                          url_end="https://x/dashboard"),
                              {"type": "positive"})[0]
    assert positive["skipped"] is True

    negative = rules.evaluate([rule], _Ev(url_start="https://x/auth/login",
                                          url_end="https://x/dashboard"),
                              {"type": "negative"})[0]
    assert negative["skipped"] is False and negative["ok"] is False

