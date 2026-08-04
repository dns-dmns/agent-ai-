"""Unit tests for the platform modules: API seeding, history DB, schedules.

These run against a temporary workspace so they never touch real project data.
"""

import importlib
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
