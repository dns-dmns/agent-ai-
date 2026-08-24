"""Unit tests for the platform modules: API seeding, history DB, schedules.

These run against a temporary workspace so they never touch real project data.
"""

import importlib
import json
import os
import sys
import tempfile

import pytest
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



def test_column_not_empty_rule_round_trip_and_generation():
    """An audit trail with blank Date/User is useless even though every row is
    present — statable once as a rule, enforced on every run."""
    rule = rules.validate({
        "title": "Audit history always shows date and user",
        "kind": "column_not_empty", "selector": "#auditTable",
        "value": "Date/Time, User", "severity": "blocker",
        "generate_scenario": True,
    })
    assert rule["kind"] == "column_not_empty"

    import pytest
    with pytest.raises(ValueError, match="column_not_empty"):
        rules.validate({"title": "x", "kind": "column_not_empty"})

    generated = rules.to_scenarios([rule])
    assert generated and generated[0]["steps"][0]["action"] == "VERIFY_COLUMN_NOT_EMPTY"
    assert generated[0]["steps"][0]["value"] == "Date/Time, User"


def test_column_not_empty_rule_reads_the_assertion_result():
    passing = _Ev()
    passing.assertions = [{"action": "VERIFY_COLUMN_NOT_EMPTY", "ok": True,
                           "detail": "every row has data"}]
    failing = _Ev()
    failing.assertions = [{"action": "VERIFY_COLUMN_NOT_EMPTY", "ok": False,
                           "detail": "Date/Time (blank in 5/5 rows)"}]
    rule = rules.validate({"title": "audit", "kind": "column_not_empty",
                           "value": "Date/Time, User"})

    assert rules.evaluate([rule], passing, {"type": "positive"})[0]["ok"] is True
    bad = rules.evaluate([rule], failing, {"type": "positive"})[0]
    assert bad["ok"] is False and "blank in 5/5" in bad["detail"]

    # A scenario that never looked is skipped, not silently passed.
    assert rules.evaluate([rule], _Ev(), {"type": "positive"})[0]["skipped"] is True


# ── Generic handling of application quirks (Circular Machine findings) ──
def test_control_synonyms_cover_the_words_applications_actually_use():
    """A recorded "Save" must still find the button on a screen that says
    "Update", and an Export that is only an icon."""
    from app import heuristics

    save = heuristics.synonym_selectors('button:has-text("Save")')
    assert 'button:has-text("Update")' in save
    assert 'button:has-text("Submit")' in save
    assert 'button:has-text("Save")' not in save          # no pointless retry

    reject = heuristics.synonym_selectors('p-confirmdialog button:has-text("No")')
    assert 'p-confirmdialog button:has-text("Cancel")' in reject
    assert 'p-confirmdialog button:has-text("Close")' in reject

    export = heuristics.synonym_selectors('button:has-text("Export")')
    assert "button:has(i.pi-file-excel)" in export        # unlabelled icon button
    assert any("title*='Export'" in s for s in export)

    # A confirm button must never be offered as an alternative for a reject one.
    assert 'p-confirmdialog button:has-text("Yes")' not in reject
    # Nothing to expand when the selector has no label.
    assert heuristics.synonym_selectors("#saveBtn") == []


def test_empty_state_wording_is_not_a_test_failure():
    """"No records found" vs "No data available" is a framework difference, not
    an application defect."""
    from app import executor

    assert executor.looks_like_empty_state("No records found") is True
    assert executor.looks_like_empty_state("Multi-Feed Circular") is False
    assert executor.find_empty_state("  No data available  ") == "No data available"
    assert executor.find_empty_state("Name Width Grade") == ""


def test_input_refused_by_the_widget_is_the_app_validating():
    """A numeric field that drops letters enforces its rule at the input layer.
    Reporting "the invalid input was accepted" would be exactly backwards."""
    from app import executor

    assert executor._same_input("2344", "2,344.00") is True     # reformatting
    assert executor._same_input(" abc ", "abc") is True          # trimming
    assert executor._same_input("abc", "") is False              # refused
    assert executor._same_input("12345", "1234") is False        # truncated

    from app import verdict
    ev = verdict.Evidence(steps_total=6, steps_executed=6)
    ev.input_rejected = [{"selector": "#machineWidth", "typed": "abcd", "kept": "",
                          "detail": "#machineWidth refused the value outright — "
                                    "the field is still empty"}]
    result = verdict.evaluate(
        "The application should reject non-numeric input.", ev, "negative")
    assert result.status == verdict.PASS
    assert "rejected the input" in result.reason

    # The same evidence must NOT rescue a scenario that expected a save.
    positive = verdict.evaluate(
        "The machine is created successfully.", ev, "positive")
    assert positive.status != verdict.PASS


def test_a_real_error_message_still_wins_over_the_input_layer_reason():
    from app import verdict
    ev = verdict.Evidence(steps_total=6, steps_executed=6)
    ev.input_rejected = [{"selector": "#width", "typed": "abc", "kept": "",
                          "detail": "refused"}]
    ev.add_message("Machine Width must be numeric", ".p-error")
    result = verdict.evaluate(
        "An error 'Machine Width must be numeric' is shown.", ev, "negative")
    assert result.status == verdict.PASS
    assert "Machine Width must be numeric" in result.reason


# ── §2 layer 3: the learned message surface is actually used ───────────
def test_learned_message_selectors_are_read_back_into_the_scan():
    """The platform was recording where messages appear and then scanning
    without that knowledge, so an app whose error class is in no framework list
    stayed invisible and a correct app was reported as a false FAIL."""
    from app import executor, learned

    learned.remember_message_selector("SurfaceTest", ".totally-bespoke-error")
    assert ".totally-bespoke-error" in learned.message_selectors("SurfaceTest")

    # The scan script must accept them, and the seed list must survive.
    assert executor.MESSAGE_SCAN_JS.startswith("(extra) =>")
    assert "...(extra || [])" in executor.MESSAGE_SCAN_JS
    assert ".oxd-input-field-error-message" in executor.MESSAGE_SCAN_JS

    # A full path is too brittle to reuse; only the class part is kept.
    learned.remember_message_selector("SurfaceTest", "div.wrapper > span.my-err")
    kept = learned.message_selectors("SurfaceTest")
    assert ".my-err" in kept
    assert "div.wrapper" not in kept


def test_a_variable_is_never_uniquified():
    """§5a — the actual bug behind duplicate tests that pass without testing.
    Reusing a captured value is deliberate: it is how the second submission
    matches the first."""
    from app import executor

    assert executor.make_dep_value_unique(
        "#custName", "{{dup_name}}", "abc123") == "{{dup_name}}"
    assert executor.make_dep_value_unique(
        "#custName", "ACME {{run_token}}", "abc123") == "ACME {{run_token}}"
    # A plain literal is still made unique, which is what stops replay collisions.
    assert executor.make_dep_value_unique("#custName", "ACME", "abc123") == "ACMEabc123"
    # And credentials / dates are still left alone, as before.
    assert executor.make_dep_value_unique("#username", "Admin", "abc123") == "Admin"
    assert executor.make_dep_value_unique("#date", "2026-08-03", "abc123") == "2026-08-03"


def test_mandatory_fields_include_the_ones_the_recording_also_touched():
    """The bug: de-duplication dropped a learned field's `required` flag whenever
    the recording had described the same field, so MANDATORY FIELDS listed only
    the fields the tester had NOT touched — exactly backwards."""
    import importlib
    from app import learned, main as main_module
    importlib.reload(learned)

    learned.remember_fields("MandatoryTest", "Machines", [
        {"selector": "#machineName", "label": "Machine Name *", "required": True},
        {"selector": "#machineType", "label": "Machine Type *", "required": True},
        {"selector": "#status", "label": "Status *", "required": True},
    ])
    # The recording touched two of the three.
    actions = [
        {"action": "TYPE", "selector": "#machineName", "tag": "input", "label": "Machine Name *"},
        {"action": "TYPE", "selector": "#machineType", "tag": "input", "label": "Machine Type *"},
        {"action": "CLICK", "selector": "#saveBtn", "tag": "button", "text": "Save"},
    ]
    context = main_module.build_fields_context(actions, "MandatoryTest", "Machines")
    mandatory = [line for line in context.splitlines()
                 if line.startswith("MANDATORY FIELDS:")]
    assert mandatory, context
    for selector in ("#machineName", "#machineType", "#status"):
        assert selector in mandatory[0], f"{selector} missing from {mandatory[0]!r}"


def test_recorded_effects_reach_the_generation_prompt():
    """§1 — the agents must assert the wording the application really showed."""
    from app import main as main_module

    actions = [
        {"action": "CLICK", "selector": "#saveBtn", "tag": "button", "text": "Save",
         "effect": {"url_before": "/new", "url_after": "/list",
                    "messages": [{"text": "Customer ACME saved successfully",
                                  "selector": "div.zx-note-9"}],
                    "appeared": ["#customerCode"], "disappeared": []}},
    ]
    described = main_module.describe_recorded_effects(actions)
    assert "Customer ACME saved successfully" in described
    assert "div.zx-note-9" in described
    assert "/list" in described
    assert "do not invent" in described

    # A recording made before effect capture existed yields nothing, and the
    # prompt is unchanged for it.
    assert main_module.describe_recorded_effects(
        [{"action": "CLICK", "selector": "#saveBtn"}]) == ""


# ── §5b/c: unique_by and the two-phase duplicate pattern ───────────────
def test_duplicate_scenarios_repeat_the_whole_uniqueness_key():
    """A duplicate test used to prove nothing: uniquification made the second
    submission unique too. Phase 1 creates a fresh original and phase 2 repeats
    every key field from stored variables, which are never re-tokenised."""
    from app import contracts

    contracts.save("DupTest", "Customers",
                   {"unique_by": ["#custName", "#effectiveDate"], "run_once": False})
    scenarios, notes = contracts.duplicate_scenarios(
        "DupTest", "Customers",
        open_steps=[{"action": "CLICK", "selector": "#newBtn"}],
        submit_selector="#saveBtn")
    assert notes == []
    assert len(scenarios) == 2

    dup = scenarios[0]
    actions = [(s["action"], s.get("selector", ""), s.get("value", "")) for s in dup["steps"]]
    # Phase 1 stores both key fields...
    assert ("STORE", "#custName", "dup_custname") in actions
    assert ("STORE", "#effectiveDate", "dup_effectivedate") in actions
    # ...and phase 2 replays both from those variables, the date included.
    assert ("TYPE", "#custName", "{{dup_custname}}") in actions
    assert ("TYPE", "#effectiveDate", "{{dup_effectivedate}}") in actions

    # The bonus case: change only the last key field and the save must succeed.
    varied = scenarios[1]
    assert varied["type"] == "positive"
    assert "#dupDate" not in varied["scenario_name"]
    assert ("TYPE", "#custName", "{{dup_custname}}") in [
        (s["action"], s.get("selector", ""), s.get("value", "")) for s in varied["steps"]]


def test_a_duplicate_expectation_is_not_misread_as_a_negative_one():
    """The generated wording feeds the intent classifier. "Refusing it would
    mean…" in an explanatory aside made a correct save read as expecting an
    error, and the scenario failed for behaving properly."""
    from app import contracts, verdict

    contracts.save("DupWording", "Customers",
                   {"unique_by": ["#name", "#date"], "run_once": False})
    scenarios, _ = contracts.duplicate_scenarios("DupWording", "Customers")

    negative = verdict.classify_expectation(scenarios[0]["expected_result"])
    assert negative["expects_error"] is True

    positive = verdict.classify_expectation(scenarios[1]["expected_result"])
    assert positive["expects_success"] is True
    assert positive["expects_error"] is False, scenarios[1]["expected_result"]


def test_unique_by_survives_a_contract_round_trip():
    from app import contracts

    saved = contracts.save("DupRound", "Customers",
                           {"unique_by": ["#a", " #b ", ""], "run_once": True})
    assert saved["unique_by"] == ["#a", "#b"]
    assert contracts.load("DupRound", "Customers")["unique_by"] == ["#a", "#b"]

    # A flow with no key says so instead of generating a test that proves nothing.
    contracts.save("DupRound", "Other", {"run_once": True})
    scenarios, notes = contracts.duplicate_scenarios("DupRound", "Other")
    assert scenarios == []
    assert notes and "unique_by" in notes[0]


# ── Generation must be grounded in what was actually recorded ──────────
def test_a_login_only_recording_cannot_generate_a_master_data_suite():
    """The real failure this prevents: a flow named 'login' was launched at
    /masters/beamer-capacity, the app bounced to the login screen, and the
    recording captured only the credentials. Generation then produced 17
    scenarios in which all 27 selectors were invented from the URL alone."""
    import pytest
    from fastapi import HTTPException
    from app import main as main_module

    login_only = [
        {"action": "CLICK", "selector": "#username", "label": "Username"},
        {"action": "TYPE", "selector": "#username", "value": "d", "label": "Username"},
        {"action": "CLICK", "selector": "#password", "label": "Password"},
        {"action": "TYPE", "selector": "#password", "value": "123456", "label": "Password"},
        {"action": "CLICK", "selector": 'button:has-text("Login")'},
    ]
    grounding = main_module.recording_grounding(login_only)
    assert grounding["credentials_only"] is True
    assert grounding["business_fields"] == []

    with pytest.raises(HTTPException) as raised:
        main_module.assert_recording_can_ground_generation(
            "GroundTest", "login", login_only,
            "https://erp.example.com/ambica/pages/masters/beamer-capacity")
    assert raised.value.status_code == 400
    assert "only captured a login" in raised.value.detail
    assert "Re-record" in raised.value.detail


def test_a_genuine_login_flow_is_still_allowed():
    """A login flow IS credentials-only, and that is correct — the guard must
    not block the one flow that legitimately looks like this."""
    from app import main as main_module

    login_only = [
        {"action": "TYPE", "selector": "#username", "value": "admin", "label": "Username"},
        {"action": "TYPE", "selector": "#password", "value": "pw", "label": "Password"},
        {"action": "CLICK", "selector": 'button:has-text("Login")'},
    ]
    # No exception: the target URL is the login page.
    main_module.assert_recording_can_ground_generation(
        "GroundTest", "login", login_only, "https://erp.example.com/auth/login")

    # And a recording that reached a real form is always allowed.
    real = login_only + [
        {"action": "TYPE", "selector": "#machineName", "value": "M1", "label": "Machine Name *"},
    ]
    main_module.assert_recording_can_ground_generation(
        "GroundTest", "Beamer Capacity", real,
        "https://erp.example.com/pages/masters/beamer-capacity")


def test_a_recording_that_reached_the_form_is_not_blocked_even_without_login():
    from app import main as main_module

    business = [
        {"action": "CLICK", "selector": "#addBtn", "text": "Add"},
        {"action": "TYPE", "selector": "#beamerCode", "value": "BC1", "label": "Code *"},
        {"action": "CLICK", "selector": "#saveBtn", "text": "Save"},
    ]
    assert main_module.recording_grounding(business)["credentials_only"] is False
    main_module.assert_recording_can_ground_generation(
        "GroundTest", "Beamer Capacity", business,
        "https://erp.example.com/pages/masters/beamer-capacity")


def test_grounding_reports_how_much_of_a_suite_was_invented():
    """A model may name a Save button nobody recorded. It may not invent a whole
    form — and the report has to say which happened."""
    from app import main as main_module

    actions = [
        {"action": "TYPE", "selector": "#username", "selectors": ["#username", "input#username"]},
        {"action": "TYPE", "selector": "#password"},
    ]
    fabricated = [{"scenario_name": "Create Beamer Capacity", "steps": [
        {"action": "TYPE", "selector": "#beamerCode", "value": "X"},
        {"action": "TYPE", "selector": "#capacity", "value": "1"},
        {"action": "CLICK", "selector": "#saveBtn"},
    ]}]
    report = main_module.generation_grounding(actions, fabricated)
    assert report["invented_percent"] == 100
    assert report["grounded"] is False
    assert "#beamerCode" in report["invented"]

    honest = [{"scenario_name": "Log in", "steps": [
        {"action": "TYPE", "selector": "#username", "value": "a"},
        {"action": "TYPE", "selector": "#password", "value": "b"},
        {"action": "CLICK", "selector": "#loginBtn"},
    ]}]
    good = main_module.generation_grounding(actions, honest)
    assert good["grounded"] is True                # one guessed button is fine
    assert good["invented"] == ["#loginBtn"]


def test_every_web_script_parses():
    """A syntax error in an adapter file silently disables every function in it:
    `ui-coverage.js` failed to parse and the Coverage panel simply did nothing,
    which surfaced only as a 30-second Playwright timeout in smoke_ui. Parsing
    is a millisecond check that names the file and line instead."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is not installed; smoke_ui still covers this indirectly")

    web = Path(__file__).resolve().parent.parent / "web"
    scripts = sorted(web.glob("*.js"))
    assert scripts, "no web scripts found — has the layout changed?"
    for script in scripts:
        result = subprocess.run([node, "--check", str(script)],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"{script.name} does not parse:\n{result.stderr}"


# ── Quota handling: a daily cap must never be retried ──────────────────
DAILY_429 = (
    "429 RESOURCE_EXHAUSTED. quota_metric: "
    '"generativelanguage.googleapis.com/generate_content_free_tier_requests" '
    'quota_id: "GenerateRequestsPerDayPerProjectPerModel-FreeTier" quota_value: 20, '
    "retry_delay { seconds: 28 }"
)
MINUTE_429 = (
    "429 RESOURCE_EXHAUSTED. "
    'quota_id: "GenerateRequestsPerMinutePerProjectPerModel-FreeTier" quota_value: 10, '
    "retry_delay { seconds: 28 }"
)


def test_a_daily_quota_error_is_told_apart_from_a_per_minute_one():
    """Retrying a *daily* cap cannot succeed inside a 30s backoff, and on a free
    tier of 20 requests each attempt spends one of them: three retries turned one
    exhausted call into four."""
    from app import llm

    assert llm.classify_quota_error(DAILY_429) == "daily"
    assert llm.classify_quota_error(MINUTE_429) == "per_minute"
    assert llm.classify_quota_error("503 Service Unavailable: overloaded") == ""
    assert llm.classify_quota_error("400 Invalid argument") == ""
    # An unlabelled 429 gets the safe treatment: wait, do not give up.
    assert llm.classify_quota_error("429 Too Many Requests") == "per_minute"


def test_googles_own_retry_delay_is_honoured():
    from app import llm

    assert llm.suggested_retry_delay(MINUTE_429, 4.0) == 29.0
    # No delay named: keep the caller's backoff.
    assert llm.suggested_retry_delay("429 Too Many Requests", 4.0) == 4.0
    # And never wait absurdly long.
    assert llm.suggested_retry_delay("retry_delay { seconds: 9999 }", 4.0) == 65.0


def test_the_quota_message_never_blames_the_api_key():
    """The old message sent people to check a key that was working perfectly."""
    from app import llm

    daily = llm.quota_message("gemini-3.6-flash", "daily")
    assert "Nothing is wrong with your API key" in daily
    assert "midnight" in daily and "Pacific" in daily
    assert "no key at all" in daily          # running suites needs no key
    assert "gemini-3.6-flash" in daily

    minute = llm.quota_message("gemini-3.6-flash", "per_minute")
    assert "rate-limited" in minute and "Wait a minute" in minute


def test_the_budget_guard_warns_before_a_run_that_cannot_finish(monkeypatch):
    """Three agents over three rounds is nine requests. Saying so first beats
    failing half-way and leaving a partial suite behind."""
    from app import config, llm

    monkeypatch.setattr(llm, "calls_today", lambda model_name="": 14)
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 20)

    ok = llm.budget_status("m", planned_calls=1)
    assert ok["remaining"] == 6 and ok["enough"] is True and ok["exhausted"] is False

    tight = llm.budget_status("m", planned_calls=9)
    assert tight["enough"] is False and tight["exhausted"] is False

    monkeypatch.setattr(llm, "calls_today", lambda model_name="": 25)
    spent = llm.budget_status("m", planned_calls=1)
    assert spent["remaining"] == 0 and spent["exhausted"] is True

    # Billing enabled ⇒ no assumed cap ⇒ never second-guess the user.
    monkeypatch.setattr(config, "LLM_DAILY_BUDGET", 0)
    unlimited = llm.budget_status("m", planned_calls=99)
    assert unlimited["enough"] is True and unlimited["exhausted"] is False
    assert unlimited["remaining"] is None


def test_vision_healing_is_opt_in_so_runs_do_not_spend_generation_quota():
    """A run with several broken selectors could consume a whole day's allowance
    through vision healing, so *running* tests silently competed with
    *generating* them."""
    import subprocess
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    probe = "import app.config as c; print('VALUE=' + str(c.VISION_HEALING_ENABLED))"

    def run(cwd, **extra_env):
        # cwd matters: config.py calls load_dotenv(), which walks up from the
        # working directory — so the repo's own .env would override the default.
        env = {k: v for k, v in os.environ.items() if k != "QA_VISION_HEALING"}
        env["PYTHONPATH"] = str(root)
        env.update(extra_env)
        done = subprocess.run([_sys.executable, "-c", probe], capture_output=True,
                              text=True, cwd=str(cwd), env=env, timeout=60)
        assert "VALUE=" in done.stdout, done.stdout + done.stderr
        return done.stdout.split("VALUE=")[1].strip()

    with tempfile.TemporaryDirectory() as outside:
        assert run(outside) == "False", "vision healing must default to off"
        assert run(outside, QA_VISION_HEALING="1") == "True", "the env var must still win"


# ── Defect log: an additional feature, disconnected from the main flow ──
DEFECT_CSV = (
    "Defect ID,Date,Module,Description,Steps to Reproduce,Expected Result,"
    "Actual Result,Priority,Status\r\n"
    'AM-001,03-06-2026,Master Data -> Paper Core,No validation of diameters,'
    '"1. Navigate to Master Data -> Paper Core.\n'
    "2. Click to create a new Paper Core.\n"
    "3. Enter an Inner Diameter (mm) value.\n"
    "4. Enter an Outer Diameter (mm) value that is less than or equal to the Inner Diameter.\n"
    '5. Attempt to save/submit.",'
    '"System should validate that Outer Diameter (mm) is strictly greater than Inner '
    'Diameter (mm). If not, a validation error should be shown and submission blocked.",'
    "No validation exists,Medium,Open\r\n"
    'AM-002,03-06-2026,Master Data -> Beam Length,Label typo,'
    '"1. Navigate to Master Data -> Beam Length.\n'
    '2. Observe the field labelled ""Beam Dish""."'
    ',"The field should be labelled ""Beam Disk"" (not ""Beam Dish"")."'
    ',Wrong label,Low,Open\r\n'
)

PAPER_FIELDS = [
    {"selector": 'input[formcontrolname="innerDiameter"]',
     "label": "Inner Diameter (mm) *", "kind": "number"},
    {"selector": 'input[formcontrolname="outerDiameter"]',
     "label": "Outer Diameter (mm) *", "kind": "number"},
]


def test_a_defect_sheet_is_parsed_whatever_the_header_spelling():
    from app import defects

    records = defects.read_sheet(DEFECT_CSV.encode("utf-8"), "log.csv")
    assert [r["defect_id"] for r in records] == ["AM-001", "AM-002"]
    assert records[0]["module"] == "Master Data -> Paper Core"
    assert records[0]["priority"] == "Medium"
    assert records[0]["is_open"] is True
    assert "strictly greater" in records[0]["expected"]

    # A sheet without the two required columns says which are missing.
    import pytest
    with pytest.raises(defects.DefectImportError, match="header row"):
        defects.read_sheet(b"Foo,Bar\r\n1,2\r\n", "log.csv")
    with pytest.raises(defects.DefectImportError, match="openpyxl|not supported"):
        defects.read_sheet(b"nonsense", "log.xls")


def test_a_module_label_maps_to_a_recorded_flow_or_to_nothing():
    """"Master Data -> Paper Core" is the flow "Paper Core". A defect aimed at
    the wrong module would fail for reasons unrelated to the defect, so a poor
    guess is worse than no match."""
    from app import defects

    flows = ["Paper Core", "Beam Length", "Circular Machine", "Paper Core [defects]"]
    assert defects.match_flow("Master Data -> Paper Core", flows) == "Paper Core"
    assert defects.match_flow("Paper Core", flows) == "Paper Core"
    assert defects.match_flow("Masters / Beam Length", flows) == "Beam Length"
    assert defects.match_flow("Master Data -> Customer Master", flows) == ""
    assert defects.match_flow("", flows) == ""
    # A [defects] flow is never itself a target.
    assert not defects.match_flow("Paper Core", flows).endswith("[defects]")


def test_prose_steps_resolve_by_label_and_are_never_guessed():
    """Field names resolve against the labels the engine already inventoried —
    free and exact. A step the sheet leaves relative is reported, not invented."""
    from app import defects

    records = defects.read_sheet(DEFECT_CSV.encode("utf-8"), "log.csv")
    built = defects.build_scenario(records[0], PAPER_FIELDS,
                                   add_selector="#addBtn", submit_selector="#saveBtn")
    assert built["runnable"] is False
    assert len(built["unresolved"]) == 2
    assert any("relative terms" in u for u in built["unresolved"])
    # Suggestions are offered for acceptance, and satisfy "outer <= inner".
    assert built["suggestions"]['input[formcontrolname="innerDiameter"]'] == "50"
    assert built["suggestions"]['input[formcontrolname="outerDiameter"]'] == "40"
    # No TYPE step was emitted with an empty value.
    assert not any(s["action"] == "TYPE" and not s.get("value")
                   for s in built["scenario"]["steps"])

    # With the values accepted it becomes runnable, and only then.
    accepted = defects.build_scenario(
        records[0], PAPER_FIELDS, add_selector="#addBtn", submit_selector="#saveBtn",
        data={'input[formcontrolname="innerDiameter"]': "50",
              'input[formcontrolname="outerDiameter"]': "40"})
    assert accepted["runnable"] is True
    typed = [(s["selector"], s["value"]) for s in accepted["scenario"]["steps"]
             if s["action"] == "TYPE"]
    assert typed == [('input[formcontrolname="innerDiameter"]', "50"),
                     ('input[formcontrolname="outerDiameter"]', "40")]


def test_an_observation_defect_asserts_the_wording_the_sheet_quotes():
    """"Observe the field labelled 'Beam Dish'" must not type the label into the
    field — it must assert the *correct* wording, which the expectation quotes."""
    from app import defects

    records = defects.read_sheet(DEFECT_CSV.encode("utf-8"), "log.csv")
    beam = [{"selector": 'input[formcontrolname="beamDish"]', "label": "Beam Dish",
             "kind": "text"}]
    built = defects.build_scenario(records[1], beam, add_selector="#addBtn")
    assert built["runnable"] is True
    verify = [s for s in built["scenario"]["steps"] if s["action"] == "VERIFY_TEXT"]
    assert verify and verify[0]["value"] == "Beam Disk"
    assert not any(s["action"] == "TYPE" for s in built["scenario"]["steps"])


def test_an_open_defect_that_fails_is_not_a_regression():
    """The distinction that makes a defect report readable, computed outside the
    verdict engine so PASS/FAIL keeps its meaning."""
    from app import defects

    assert defects.outcome("FAIL", known_defect=True) == "still_open"
    assert defects.outcome("PASS", known_defect=True) == "fixed"
    assert defects.outcome("FAIL", known_defect=False) == "regressed"
    assert defects.outcome("PASS", known_defect=False) == "holding"
    assert defects.outcome("UNVERIFIED", known_defect=True) == "unverified"
    assert defects.outcome("", known_defect=True) == "unverified"


def test_the_defect_log_merges_by_id_and_keeps_accepted_values():
    from app import defects

    stored = [{"defect_id": "AM-001", "status": "Open", "data": {"#a": "50"}}]
    incoming = [{"defect_id": "AM-001", "status": "Closed"},
                {"defect_id": "AM-009", "status": "Open"}]
    merged, added, updated = defects.merge(stored, incoming)
    assert (added, updated) == (1, 1)
    by_id = {d["defect_id"]: d for d in merged}
    assert by_id["AM-001"]["status"] == "Closed"
    # A re-import must not discard values the user accepted earlier.
    assert by_id["AM-001"]["data"] == {"#a": "50"}


def test_an_xlsx_defect_sheet_parses_the_same_as_csv():
    """openpyxl is declared in requirements.txt but was not actually installed,
    so .xlsx upload failed and .xlsx export row-counting was silently degraded."""
    import io
    from app import defects

    openpyxl = pytest.importorskip("openpyxl")

    book = openpyxl.Workbook()
    sheet = book.active
    # Real exports often carry a title row above the header.
    sheet.append(["Ambica bug list"])
    sheet.append([])
    sheet.append(["Defect ID", "Module", "Steps to Reproduce", "Expected Result",
                  "Priority", "Status"])
    sheet.append(["AM-002", "Master Data -> Beam Length",
                  '1. Observe the field labelled "Beam Dish".',
                  'The field should be labelled "Beam Disk".', "Low", "Open"])
    buffer = io.BytesIO()
    book.save(buffer)

    records = defects.read_sheet(buffer.getvalue(), "Ambica bug list.xlsx")
    assert len(records) == 1
    assert records[0]["defect_id"] == "AM-002"
    assert records[0]["module"] == "Master Data -> Beam Length"
    assert records[0]["is_open"] is True
    assert "Beam Disk" in records[0]["expected"]


def test_openpyxl_is_importable_so_xlsx_paths_work():
    """Two features depend on it: defect-sheet import and counting the rows in a
    downloaded .xlsx export."""
    import importlib.util
    assert importlib.util.find_spec("openpyxl") is not None, (
        "openpyxl is missing — run 'pip install -r requirements.txt'. "
        "Without it .xlsx defect sheets cannot be imported and VERIFY_DOWNLOAD "
        "cannot count rows in an .xlsx export."
    )


# ── Project export: a separate, strictly read-only feature ─────────────
EXPORT_MODULE = {
    "flow": "Circular Machine",
    "target_url": "https://erp.example.com/masters/circular",
    "fields": [
        {"selector": 'input[formcontrolname="machineName"]', "label": "Machine Name *",
         "kind": "text", "required": True,
         "selectors": ['input[formcontrolname="machineName"]', "#machineName"]},
        {"selector": 'p-dropdown[formcontrolname="status"]', "label": "Status *",
         "kind": "dropdown", "required": True},
    ],
    "actions": [
        {"action": "CLICK", "selector": "#addBtn", "text": "Add New"},
        {"action": "CLICK", "selector": 'button:has-text("Save")', "text": "Save"},
    ],
    "scenarios": [{
        "scenario_name": "Create machine with all valid fields",
        "expected_result": "The machine is created and appears in the table.",
        "steps": [
            {"action": "CLICK", "selector": "#addBtn"},
            {"action": "TYPE", "selector": 'input[formcontrolname="machineName"]',
             "value": "CM-01"},
            {"action": "SELECT", "selector": 'p-dropdown[formcontrolname="status"]',
             "value": "Active"},
            {"action": "CLICK", "selector": 'button:has-text("Save")'},
            {"action": "VERIFY_MESSAGE", "value": "added successfully"},
            {"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": "CM-01"},
        ],
    }],
}


def test_export_produces_the_documented_folder_structure():
    from app import export_project

    files = export_project.build_project("Ambica", [EXPORT_MODULE])
    for expected in ("pages/BasePage.js", "pages/CircularMachinePage.js",
                     "tests/circular-machine.spec.js",
                     "fixtures/baseTest.js",
                     "test-data/circular-machine.json", "test-data/credentials.json",
                     "utils/credentials.js", "utils/unique.js",
                     "playwright.config.js", "package.json", "README.md",
                     ".env.example", ".gitignore"):
        assert expected in files, f"missing {expected} — got {sorted(files)}"


def test_the_page_object_declares_locators_and_the_spec_never_touches_one():
    """The rule from the reference project: locators live in the page object;
    spec files only call its methods."""
    from app import export_project

    files = export_project.build_project("Ambica", [EXPORT_MODULE])
    page = files["pages/CircularMachinePage.js"]
    spec = files["tests/circular-machine.spec.js"]

    assert "class CircularMachinePage extends BasePage" in page
    assert "this.machineName = page.locator(" in page
    # A control with more than one recorded selector becomes a.or(b).
    assert ".or(page.locator(" in page
    # The spec calls methods and never a raw selector.
    assert "createMachineWithAllValidFields(" in spec
    assert "page.locator(" not in spec
    assert "formcontrolname" not in spec


def test_generated_code_follows_the_house_rules():
    from app import export_project

    page = export_project.build_project("Ambica", [EXPORT_MODULE])[
        "pages/CircularMachinePage.js"]

    # A click followed by an assertion may be swallowed — the assertion is the
    # real check. A plain click must NOT be, or a broken step passes silently.
    assert "await this.btnSave.click({ timeout: 10_000 })" in page
    assert "await this.btnAddNew.click();" in page

    # Nothing is counted or matched before it is waited for.
    row_check = page[page.index("filter({ hasText:") - 400:]
    assert "waitFor({ state: 'visible' })" in row_check

    # Typed values come from the test-data file, falling back to the recording.
    assert 'fill(String(data.machineName ?? "CM-01"))' in page

    # A step the engine handled internally is a visible TODO, never silent.
    audit = dict(EXPORT_MODULE)
    audit["scenarios"] = [{"scenario_name": "Audit history", "expected_result": "x",
                           "steps": [{"action": "VERIFY_COLUMN_NOT_EMPTY",
                                      "selector": "#auditTable", "value": "Date/Time"}]}]
    generated = export_project.build_project("A", [audit])["pages/CircularMachinePage.js"]
    assert "TODO (VERIFY_COLUMN_NOT_EMPTY)" in generated


def test_the_spec_sets_a_timeout_and_reads_its_data_file():
    from app import export_project

    spec = export_project.build_project("Ambica", [EXPORT_MODULE])[
        "tests/circular-machine.spec.js"]
    assert "test.setTimeout(" in spec
    assert "require('../test-data/circular-machine.json')" in spec
    # Credentials come through the fixture, not a direct require — one place
    # (utils/credentials.js) decides env-var vs file.
    assert "require('../fixtures/baseTest')" in spec
    assert "async ({ page, credentials })" in spec


def test_the_readme_marks_which_data_changes_every_run():
    """The reference project calls stale hand-copied data its biggest recurring
    failure, so the export computes that column instead of leaving it to a human."""
    from app import export_project

    volatile = json.loads(json.dumps(EXPORT_MODULE))
    volatile["scenarios"][0]["steps"][1]["value"] = "{{order_code}}"
    readme = export_project.build_project("Ambica", [volatile])["README.md"]
    assert "Changes every run?" in readme
    assert "**Yes**" in readme

    steady = export_project.build_project("Ambica", [EXPORT_MODULE])["README.md"]
    assert "| No |" in steady


def test_credentials_are_never_exported():
    from app import export_project

    files = export_project.build_project("Ambica", [EXPORT_MODULE])
    assert json.loads(files["test-data/credentials.json"]) == {"username": "", "password": ""}
    assert "QA_PASSWORD=\n" in files[".env.example"]
    assert ".env" in files[".gitignore"]


# ══════════════════════════════════════════════════════════════════════
# Mandatory-field capture — one implementation, and it actually runs
# ══════════════════════════════════════════════════════════════════════
# "It is not capturing the mandatory fields" survived four attempted fixes
# because two independent things were wrong and fixing either alone changed
# nothing visible:
#   1. Two detectors. recorder.py demanded ng-invalid AND ng-pristine; the
#      engine accepted a trailing asterisk. Same control, opposite answers.
#   2. The engine's inventory only ran after a Save click had already failed on
#      a disabled button, so a form that saves correctly never produced one.

def test_there_is_only_one_required_field_detector():
    root = Path(__file__).resolve().parent.parent / "app"
    recorder_src = (root / "recorder.py").read_text(encoding="utf-8")
    executor_src = (root / "executor.py").read_text(encoding="utf-8")

    for name, src in (("recorder", recorder_src), ("executor", executor_src)):
        assert "SHARED_PROBES_JS" in src, f"{name} does not use the shared probe"
        assert "qaRequiredInfo" in src or "qaIsRequired" in src, name

    # The recorder must delegate, not re-implement. Checked against the function
    # body rather than the whole file, so the comment explaining the old bug is
    # still allowed to name it.
    body = recorder_src[recorder_src.index("function isRequired(el)"):]
    body = body[:body.index("\n\n")]
    assert "qaIsRequired(el)" in body, f"recorder.isRequired stopped delegating: {body}"
    assert "ng-pristine" not in body, (
        "recorder has re-grown its own required test; it will drift from the engine")

    # An end-anchored asterisk cannot see "*Field" or a CSS-drawn marker. Both
    # spellings, since the engine's JS used to sit in a non-raw literal.
    for src, name in ((recorder_src, "recorder"), (executor_src, "executor")):
        for spelling in (r"/\*\s*$/", r"/\\*\\s*$/"):
            assert spelling not in src, f"{name} still uses an end-anchored asterisk test"


def test_the_shared_probe_covers_the_signals_an_angular_form_actually_uses():
    from app import dom_probes

    js = dom_probes.SHARED_PROBES_JS
    for signal in ("attr", "aria", "ng-reflect", "asterisk", "css-marker",
                   "label-class", "wrapper-class", "ng-invalid-empty"):
        assert f"'{signal}'" in js, f"missing the {signal} signal"
    # ng-invalid means "bad value" once the field is filled, so it must be
    # guarded by emptiness or every failed-validation field reads as mandatory.
    guarded = [line for line in js.splitlines()
               if "ng-invalid-empty'" in line and "return" in line]
    assert guarded and all("empty &&" in line for line in guarded), guarded


def test_the_probes_are_raw_strings():
    """The engine's JS lived in a non-raw literal where every regex needed
    doubled backslashes; one missed pair silently changed what a pattern
    matched. Raw strings remove the whole failure mode."""
    src = (Path(__file__).resolve().parent.parent / "app" / "dom_probes.py").read_text(
        encoding="utf-8")
    assert 'r"""' in src
    # Single-escaped is what a raw string gives you; the doubled spelling is the
    # non-raw form where one missed pair silently changed what a pattern matched.
    assert r"/\bng-invalid\b/" in src
    assert r"/\\bng-invalid\\b/" not in src, "not a raw string — escapes are doubled"


def test_the_form_inventory_runs_on_a_flow_that_never_fails():
    src = (Path(__file__).resolve().parent.parent / "app" / "executor.py").read_text(
        encoding="utf-8")
    assert "async def capture_form_inventory(" in src
    assert 'if action in ("CLICK", "NAVIGATE", "SELECT")' in src, (
        "the inventory must be captured on ordinary steps, not only on failure")


def test_capturing_the_inventory_cannot_break_a_run_but_is_not_silent():
    import asyncio

    from app import executor

    class ExplodingPage:
        async def evaluate(self, *_a, **_kw):
            raise RuntimeError("page closed")

    ctx: dict = {}
    assert asyncio.run(executor.capture_form_inventory(ExplodingPage(), ctx, "#x")) == []
    assert ctx.get("observed_fields") is None


def test_the_same_field_is_not_recorded_twice_per_scenario():
    import asyncio

    from app import executor

    class Page:
        async def evaluate(self, *_a, **_kw):
            return [{"selector": "#a", "required": True, "label": "A"},
                    {"selector": "#b", "required": False, "label": "B"}]

    ctx: dict = {}
    assert len(asyncio.run(executor.capture_form_inventory(Page(), ctx, ""))) == 2
    assert asyncio.run(executor.capture_form_inventory(Page(), ctx, "")) == []
    assert len(ctx["observed_fields"]) == 2


def test_a_field_stays_required_after_it_is_filled():
    """ng-invalid only holds while the control is empty, so the same field
    arrived required=True on CLICK and required=False on the TYPE that filled
    it — and whichever a consumer read last won."""
    import asyncio

    from app import main as main_mod

    session = main_mod.active_session
    saved = {k: session.get(k) for k in ("recording", "actions", "page", "start_time")}
    session.update({"recording": True, "actions": [], "start_time": 0})

    class Page:
        async def screenshot(self, **_kw):
            return b""

    session["page"] = Page()
    try:
        for event in (
            {"action": "CLICK", "selector": "#username", "required": True},
            {"action": "TYPE", "selector": "#username", "required": False, "value": "D"},
            {"action": "CLICK", "selector": "button.login", "required": False},
        ):
            asyncio.run(main_mod.handle_exposed_event(event))
        assert [a["required"] for a in session["actions"]] == [True, True, False]
    finally:
        session.update(saved)


# ══════════════════════════════════════════════════════════════════════
# Generation robustness — bad or unexpected model output is ordinary input
# ══════════════════════════════════════════════════════════════════════

def test_a_non_dict_scenario_does_not_crash_expansion():
    """A model returned a list of plain strings and coverage.has_data_table
    called .get() on one — AttributeError straight out of /analyze-flow."""
    from app import coverage

    assert coverage.has_data_table("just a string") is False
    assert coverage.has_data_table(None) is False
    mixed = ["a bare string", {"scenario_name": "Real one", "steps": ["CLICK #a"]}]
    assert [c["scenario_name"] for c in coverage.expand_scenarios(mixed)] == ["Real one"]


def test_a_lone_scenario_object_is_not_thrown_away():
    """A model with one scenario to give returns it as a bare object. The
    handler accepted only a list or {"test_cases": [...]}, so a valid test case
    became "0 test cases generated" and the log blamed the model."""
    from app.ai_agents import normalise_generated_cases as norm

    one = {"scenario_name": "Login with valid credentials", "type": "positive",
           "steps": [{"action": "CLICK", "selector": "#a"}],
           "expected_result": "redirected to home"}
    assert norm(one) == [one]
    assert len(norm([one, one])) == 2
    for key in ("test_cases", "testCases", "scenarios", "cases", "test_scenarios"):
        assert norm({key: [one]}) == [one], key


def test_normalising_never_adopts_something_that_is_not_a_scenario():
    """A dict is not a test case just because it is a dict — adopting an error
    payload would put a fabricated scenario into the suite."""
    from app.ai_agents import normalise_generated_cases as norm

    assert norm({"error": "quota exceeded"}) == []
    assert norm({"scenario_name": "Has a name but nothing else"}) == []
    assert norm(["a string", 7, None]) == []
    assert norm(None) == []
    assert norm("plain text") == []


def test_a_failed_agent_is_not_ticked_and_the_error_survives():
    source = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(
        encoding="utf-8")
    assert 'mark = "✅" if cases else "❌"' in source
    assert "agent_errors" in source
    assert "No test cases were produced. Check the API key" not in source


# ══════════════════════════════════════════════════════════════════════
# Credentials never reach a tracked file
# ══════════════════════════════════════════════════════════════════════
# actions.json carried the real login as plain literals; generation copied them
# into spec.json and test_cases.json, and the run echoed them into report.json —
# a working credential in git four times over. The recorder now substitutes
# {{username}}/{{password}} at save time and the literal lives only in the
# project's gitignored login_data.json.

def test_a_recording_is_scrubbed_and_the_literal_stored():
    from app import credentials as cs

    actions = [
        {"action": "CLICK", "selector": "#username", "label": "Username", "value": ""},
        {"action": "TYPE", "selector": "#username", "label": "Username",
         "value": "admin", "text": "admin"},
        {"action": "TYPE", "selector": "#password", "label": "Password",
         "value": "s3cret", "text": ""},
        {"action": "TYPE", "selector": "#remarks", "label": "Remarks",
         "value": "hello"},
    ]
    found = cs.scrub_recorded_actions(actions, "ScrubUnit")

    assert found == {"username": "admin", "password": "s3cret"}
    assert actions[1]["value"] == "{{username}}"
    assert actions[1]["text"] == "", "the typed echo held the literal too"
    assert actions[2]["value"] == "{{password}}"
    assert actions[3]["value"] == "hello", "an ordinary field must not be touched"
    assert cs.load_stored("ScrubUnit") == {"username": "admin", "password": "s3cret"}
    # The store must be gitignored by the repo's existing pattern.
    assert cs.store_file("ScrubUnit").name == "login_data.json"


def test_effect_messages_showing_the_username_are_masked():
    """After login the app displays the username on screen and the recorder
    captures it — the literal left through the effect block."""
    from app import credentials as cs

    actions = [
        {"action": "TYPE", "selector": "#username", "value": "admin"},
        {"action": "CLICK", "selector": "button", "value": "",
         "effect": {"messages": [{"text": "Login Successful", "selector": "x"},
                                 {"text": "admin", "selector": "span.username"}]}},
    ]
    cs.scrub_recorded_actions(actions, "ScrubEffect")
    texts = [m["text"] for m in actions[1]["effect"]["messages"]]
    assert "{{username}}" in texts
    assert "Login Successful" in texts, "ordinary wording must not be touched"


def test_generated_suites_keep_their_deliberately_wrong_values():
    """Negative cases type wrong1 / ' OR '1'='1 / spaces on purpose. Substituting
    every credential-field value would turn "invalid password is rejected" into
    typing the VALID password — only the exact literal becomes a placeholder."""
    from app import credentials as cs

    cases = [{"steps": [
        {"action": "TYPE", "selector": "#username", "value": "admin"},
        {"action": "TYPE", "selector": "#password", "value": "1234"},
        {"action": "TYPE", "selector": "#password", "value": "wrong1"},
        {"action": "TYPE", "selector": "#password", "value": "' OR '1'='1"},
    ]}]
    replaced = cs.scrub_known_literals(cases, {"username": "admin", "password": "1234"})

    values = [s["value"] for s in cases[0]["steps"]]
    assert replaced == 2
    assert values == ["{{username}}", "{{password}}", "wrong1", "' OR '1'='1"]


def test_data_rows_are_scrubbed_by_exact_literal_only():
    from app import credentials as cs

    rows = {"data": [{"username": "admin", "password": "1234", "expected": "ok"},
                     {"username": "admin", "password": "nope", "expected": "error"}]}
    cs.scrub_data_rows(rows, {"username": "admin", "password": "1234"})
    assert rows["data"][0]["password"] == "{{password}}"
    assert rows["data"][1]["password"] == "nope"
    assert rows["data"][1]["username"] == "{{username}}"


def test_the_pool_resolves_placeholders_and_rows_do_not_self_reference():
    """A scrubbed row says username: "{{username}}" — adopted verbatim it would
    resolve to itself and be typed literally."""
    from app import heuristics

    pool = {"username": "admin", "password": "1234"}
    row = {"username": "{{username}}", "password": "wrongX"}
    for key, value in row.items():
        if str(value) == "{{" + key + "}}" and key in pool:
            continue
        pool[key] = value
    assert heuristics.resolve_dynamic_variables("{{username}}", pool) == "admin"
    assert heuristics.resolve_dynamic_variables("{{password}}", pool) == "wrongX"


def test_resolve_credentials_reads_the_per_project_store(monkeypatch):
    from app import credentials as cs
    from app import executor

    cs.store("StoreUnit", username="u1", password="p1")
    monkeypatch.setattr(executor, "load_project_config", lambda _p: {})
    creds = executor.resolve_credentials("StoreUnit")
    assert creds == {"username": "u1", "password": "p1"}


def test_a_password_is_never_echoed_into_run_bookkeeping():
    """last/first_typed_value are written into report.json, which is committed —
    that echo is how a scrubbed recording still leaked the password."""
    source = (Path(__file__).resolve().parent.parent / "app" / "executor.py").read_text(
        encoding="utf-8")
    assert 'credential_store.classify_field(step) != "password"' in source


def test_new_recordings_stamp_a_schema_version():
    source = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(
        encoding="utf-8")
    assert '"schema_version": 1' in source


# ══════════════════════════════════════════════════════════════════════
# Export — a downloaded zip must be runnable, not a husk
# ══════════════════════════════════════════════════════════════════════
# A project whose flows were recorded but had no generated suite exported a zip
# with an EMPTY tests/ folder — boilerplate around nothing, reported as "the
# folder structure is wrong". And the real run of the export died on a strict-
# mode violation in BasePage before reaching any test.

def test_a_recording_only_flow_still_exports_a_runnable_smoke_spec():
    from app import export_project

    module = {"flow": "login", "target_url": "https://x.example/auth/login",
              "fields": [], "scenarios": [],
              "actions": [
                  {"action": "CLICK", "selector": "#username"},
                  {"action": "TYPE", "selector": "#username", "label": "Username",
                   "value": "{{username}}"},
                  {"action": "TYPE", "selector": "#password", "label": "Password",
                   "value": "{{password}}"},
                  {"action": "CLICK", "selector": 'button:has-text("Login")',
                   "effect": {"url_before": "https://x.example/auth/login",
                              "url_after": "https://x.example/pages/home",
                              "messages": []}},
              ]}
    files = export_project.build_project("P", [module])
    assert "tests/login.spec.js" in files, "recording-only flow was silently skipped"
    assert "pages/LoginPage.js" in files
    # The login smoke must assert the landing, not replay the login (BasePage
    # already signed in) — and never type a credential placeholder literally.
    page = files["pages/LoginPage.js"]
    assert "toHaveURL" in page
    assert "{{username}}" not in page and "{{password}}" not in page


def test_a_business_recording_smoke_keeps_steps_and_asserts_the_landing():
    from app.export_project import smoke_scenario_from_recording

    actions = [
        {"action": "CLICK", "selector": 'button:has-text("Add Item")'},
        {"action": "TYPE", "selector": "#desc", "value": "widget"},
        {"action": "CLICK", "selector": 'button:has-text("Save")',
         "effect": {"url_before": "https://x/items/new", "url_after": "https://x/items",
                    "messages": []}},
    ]
    smoke = smoke_scenario_from_recording("Item Master", actions, is_login=False)
    assert smoke and smoke["_smoke"] is True
    kinds = [s["action"] for s in smoke["steps"]]
    assert kinds[:3] == ["CLICK", "TYPE", "CLICK"]
    assert kinds[-1] == "VERIFY_URL", "the recording proved a landing; assert it"


def test_base_page_survives_an_ambiguous_recorded_selector():
    """The recorded structural selector for the password-eye toggle ALSO matched
    the 'Forgot Password?' link; a raw .click() dies on strict mode. Measured
    against the live app — TESTING-FLOW lesson 1, applied to our own generator."""
    from app.export_project import base_page_js

    steps = [{"action": "CLICK", "selector": "form span",
              "selectors": ["form span", "button.toggle"]},
             {"action": "TYPE", "selector": "#password", "label": "Password"}]
    js = base_page_js(steps, "https://x.example")
    assert ".first().click()" in js
    assert ".first().fill(credentials.password)" in js
    # Constructors have a local `page`; BasePage methods do not — every
    # locator, including the .or() argument, must go through this.page.
    assert ".or(this.page.locator(" in js
    assert "\n        await page.locator" not in js


def test_env_credentials_win_and_a_blank_file_fails_loudly():
    """Resolution lives in ONE place — utils/credentials.js, injected by the
    fixture. Env vars beat the deliberately blank credentials.json, and running
    with neither fails with instructions instead of typing nothing."""
    from app.export_project import (base_page_js, fixtures_base_test_js,
                                    utils_credentials_js)

    utils = utils_credentials_js()
    assert "process.env.TEST_USERNAME" in utils
    assert "process.env.TEST_PASSWORD" in utils
    assert "throw new Error('No credentials" in utils

    fixture = fixtures_base_test_js()
    assert "require('../utils/credentials')" in fixture
    assert "credentials:" in fixture, "the fixture must inject credentials"

    # BasePage keeps only a guard for page objects used without the fixture.
    js = base_page_js([{"action": "TYPE", "selector": "#u", "label": "user"}], "https://x")
    assert "throw new Error('No credentials" in js
    assert "process.env.TEST_USERNAME" not in js, (
        "resolution must not be duplicated in BasePage — it will drift")


def test_package_json_offers_one_command_setup_and_per_module_runs():
    import json as _json

    from app.export_project import package_json

    manifest = [{"flow": "Item Master", "spec": "item-master.spec.js"},
                {"flow": "login", "spec": "login.spec.js"}]
    scripts = _json.loads(package_json("P", manifest))["scripts"]
    assert scripts["setup"] == "npm install && npx playwright install chromium"
    assert scripts["test"] == "playwright test"
    assert scripts["test:item-master"] == "playwright test tests/item-master.spec.js"
    assert scripts["test:login"] == "playwright test tests/login.spec.js"


def test_the_downloaded_zip_has_a_single_root_folder():
    """Entries starting at pages/ and package.json scatter loose files into
    whatever directory the zip is extracted in."""
    source = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(
        encoding="utf-8")
    assert 'bundle.writestr(f"{safe_stem}/{path}", content)' in source


# ══════════════════════════════════════════════════════════════════════
# Framework runner — the second engine in Run & Verify
# ══════════════════════════════════════════════════════════════════════
# Runs the same files the export produces through stock @playwright/test.
# Assertion results only: the report must say so, live apart from the verdict
# engine's report.json, and never let an unknown outcome count as a pass.

def _pw_json(status="expected", error=""):
    """Minimal @playwright/test JSON-reporter shape, nested like the real one."""
    result = {"status": status, "duration": 1200}
    if error:
        result["error"] = {"message": error}
    return {"suites": [{"title": "login.spec.js", "suites": [{
        "title": "login",
        "specs": [{"title": "Login lands on the recorded home page (smoke)",
                   "file": "login.spec.js",
                   "tests": [{"status": status, "results": [result]}]}],
    }]}]}


def test_framework_results_map_to_the_platform_vocabulary():
    from app import framework_runner as fr

    rows = fr.parse_results(_pw_json("expected"))
    assert rows[0]["status"] == "PASS" and rows[0]["duration_ms"] == 1200

    rows = fr.parse_results(_pw_json("unexpected", "Error: boom\n  at x.js:1"))
    assert rows[0]["status"] == "FAIL"
    assert rows[0]["error"] == "Error: boom", "first line only; the stack stays in the raw file"

    assert fr.parse_results(_pw_json("skipped"))[0]["status"] == "SKIPPED"
    flaky = fr.parse_results(_pw_json("flaky"))[0]
    assert flaky["status"] == "PASS" and flaky["outcome"] == "flaky", (
        "flaky passed on retry — reported as PASS with the flakiness preserved")


def test_an_unknown_playwright_outcome_never_counts_as_a_pass():
    from app import framework_runner as fr

    rows = fr.parse_results(_pw_json("someFutureStatus"))
    assert rows[0]["status"] == "FAIL", (
        "a status this code has never seen must fail loudly, not pass silently")


def test_the_framework_report_is_labelled_and_kept_apart():
    from app import config as cfg
    from app import framework_runner as fr

    rows = fr.parse_results(_pw_json("expected"))
    report = fr.build_report("FrameworkUnit", "login", rows, 12, "run123")
    assert report["engine"] == "playwright"
    assert "UNVERIFIED" in report["engine_note"], "the limitation must be stated"
    assert report["passed_count"] == 1 and report["failed_count"] == 0

    path = fr.persist_report(report)
    assert path.name == "report.playwright.json", (
        "must never overwrite the verdict engine's report.json")
    assert path == cfg.flow_dir("FrameworkUnit", "login") / "report.playwright.json"


def test_the_runner_workspace_is_scratch_not_the_user_project():
    from app import config as cfg
    from app import framework_runner as fr

    root = fr.runner_dir("My Project!")
    assert cfg.TEMP_DIR in root.parents, "the framework lives under .tmp/"
    assert cfg.PROJECTS_DIR not in root.parents, "never inside projects/"
    assert "!" not in root.name and " " not in root.name


def test_run_framework_without_node_fails_with_instructions(monkeypatch):
    import asyncio

    from app import framework_runner as fr

    monkeypatch.setattr(fr, "node_available", lambda: "")
    lines: list[str] = []

    async def emit(message):
        lines.append(message)

    out = asyncio.run(fr.run_framework(
        "P", "", {"package.json": "{}"}, "", {}, emit, None))
    assert out is None
    blob = " ".join(lines)
    assert "nodejs.org" in blob and "Platform engine" in blob, (
        "the failure must say how to fix it AND that the other engine still works")
