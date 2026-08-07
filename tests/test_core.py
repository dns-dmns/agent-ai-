"""Unit tests for the pure logic that decides whether a test passed.

Run with:  .venv\\Scripts\\python.exe -m pytest tests -q
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import executor            # noqa: E402
from app import llm                 # noqa: E402
from app import scheduler           # noqa: E402
from app import verdict as v        # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Step DSL parser
# ──────────────────────────────────────────────────────────────────────
def test_parses_click_variants():
    assert executor.parse_step_string("CLICK ON #save") == {
        "action": "CLICK", "selector": "#save"}
    assert executor.parse_step_string("CLICK #save")["selector"] == "#save"


def test_parses_type_with_value():
    step = executor.parse_step_string('TYPE ON #name with value "Acme Corp"')
    assert step == {"action": "TYPE", "selector": "#name", "value": "Acme Corp"}


def test_parses_type_into_form():
    step = executor.parse_step_string('ENTER "Acme" into #name')
    assert step["action"] == "TYPE"
    assert step["selector"] == "#name"
    assert step["value"] == "Acme"


def test_parses_navigate_and_keypress():
    assert executor.parse_step_string("NAVIGATE TO https://x.test/app") == {
        "action": "NAVIGATE", "selector": "https://x.test/app"}
    assert executor.parse_step_string("PRESS Enter")["value"] == "Enter"


def test_parses_select_and_upload():
    step = executor.parse_step_string("SELECT Active from #status")
    assert step == {"action": "SELECT", "value": "Active", "selector": "#status"}
    step = executor.parse_step_string("UPLOAD data.csv to input[type=file]")
    assert step["action"] == "UPLOAD" and step["value"] == "data.csv"


def test_parses_assertions():
    assert executor.parse_step_string("VERIFY ON #total value > 0")["action"] == \
        "VERIFY_NON_ZERO"
    assert executor.parse_step_string("VERIFY ON #code is disabled")["action"] == \
        "VERIFY_DISABLED"
    row = executor.parse_step_string("VERIFY ON table row contains 'Acme'")
    assert row["action"] == "VERIFY_ROW_CONTAINS" and row["value"] == "Acme"
    absent = executor.parse_step_string("VERIFY table row absent 'Acme'")
    assert absent["action"] == "VERIFY_ROW_ABSENT"
    assert executor.parse_step_string("VERIFY URL contains /customers")["action"] == \
        "VERIFY_URL"


def test_dismiss_detection_ignores_stray_letters():
    """Regression: the letter 'x' used to be treated as 'close', so a failing
    click on #taxCode fell through to the Cancel button and dismissed the form."""
    assert executor.looks_like_dismiss("#cancelBtn") is True
    assert executor.looks_like_dismiss("button.p-dialog-header-close") is True
    assert executor.looks_like_dismiss("i.pi-times") is True
    assert executor.looks_like_dismiss('button[aria-label="Close"]') is True

    assert executor.looks_like_dismiss("#taxCode") is False
    assert executor.looks_like_dismiss("#exportBtn") is False
    assert executor.looks_like_dismiss("#doesNotExistAnywhere") is False
    assert executor.looks_like_dismiss("input[name='note']") is False
    assert executor.looks_like_dismiss("#maxLengthField") is False


def test_cancel_fallbacks_only_added_for_dismiss_targets():
    tax = executor.candidate_selectors({"selector": "#taxCode"}, "CLICK")
    assert not any("Cancel" in c for c in tax)
    cancel = executor.candidate_selectors({"selector": "#cancelBtn"}, "CLICK")
    assert any("Cancel" in c for c in cancel)


def test_recorded_alternate_selectors_are_tried_first():
    step = {"selector": "#pn_id_9", "selectors": ['[data-testid="save"]', "#pn_id_9"]}
    cands = executor.candidate_selectors(step, "CLICK")
    assert cands[0] == '[data-testid="save"]'
    assert "#pn_id_9" in cands


def test_unknown_text_still_becomes_a_click_for_backward_compatibility():
    assert executor.parse_step_string("button:has-text('Save')") == {
        "action": "CLICK", "selector": "button:has-text('Save')"}


def test_parse_steps_accepts_dicts_and_strings():
    steps = executor.parse_steps([
        {"action": "click", "selector": "#a"},
        "TYPE ON #b with value 'x'",
        {"selector": "#c", "value": "typed-without-action"},
    ])
    assert [s["action"] for s in steps] == ["CLICK", "TYPE", "TYPE"]


# ──────────────────────────────────────────────────────────────────────
# Expectation classification
# ──────────────────────────────────────────────────────────────────────
def test_classifies_negative_expectation():
    intent = v.classify_expectation(
        "Validation error message appears indicating the field is required")
    assert intent["expects_error"] is True
    assert intent["expects_success"] is False


def test_classifies_positive_expectation():
    intent = v.classify_expectation("Record is created successfully and appears in the grid")
    assert intent["expects_success"] is True
    assert intent["expects_error"] is False


def test_classifies_absence_and_persistence():
    assert v.classify_expectation("The record is removed from the table")["expects_absence"]
    assert v.classify_expectation("The record remains in the table")["expects_persistence"]


def test_message_classification():
    assert v.classify_message("Name is required", "small.p-error") == "error"
    assert v.classify_message("Saved successfully", ".p-toast-message-success") == "success"
    assert v.classify_message("Category created successfully") == "success"
    assert v.classify_message("Beam length must be greater than 0") == "error"


# ──────────────────────────────────────────────────────────────────────
# Verdict engine — the core correctness fix
# ──────────────────────────────────────────────────────────────────────
def test_negative_case_without_validation_message_fails():
    """The old runner marked this PASS. It must now FAIL."""
    evidence = v.Evidence(steps_total=4, steps_executed=4)
    result = v.evaluate("Validation error appears indicating the name is required",
                        evidence, "negative")
    assert result.status == v.FAIL
    assert "no" in result.reason.lower()


def test_negative_case_with_validation_message_passes():
    evidence = v.Evidence(steps_total=4, steps_executed=4)
    evidence.add_message("Name is required", "small.p-error")
    result = v.evaluate("Validation error appears indicating the name is required",
                        evidence, "negative")
    assert result.status == v.PASS
    assert "required" in result.reason.lower()


def test_positive_case_with_unexpected_error_fails():
    evidence = v.Evidence()
    evidence.add_message("Duplicate code already exists", ".p-toast-message-error")
    result = v.evaluate("Record is created successfully and appears in the table",
                        evidence, "positive")
    assert result.status == v.FAIL


def test_positive_case_with_passing_assertion_passes():
    evidence = v.Evidence()
    evidence.assertions.append(
        {"action": "VERIFY_ROW_CONTAINS", "ok": True, "detail": "found on the current page"})
    result = v.evaluate("Record is created successfully and appears in the table",
                        evidence, "positive")
    assert result.status == v.PASS
    assert result.source == "assertions"


def test_positive_case_with_no_evidence_is_unverified_not_passed():
    evidence = v.Evidence(steps_total=3, steps_executed=3)
    result = v.evaluate("Record is created successfully and appears in the table",
                        evidence, "positive")
    assert result.status == v.UNVERIFIED


def test_failed_assertion_is_decisive():
    evidence = v.Evidence()
    evidence.assertions.append(
        {"action": "VERIFY_ROW_CONTAINS", "ok": False, "detail": "'Acme' not found"})
    result = v.evaluate("Record appears in the table", evidence, "positive")
    assert result.status == v.FAIL
    assert "Acme" in result.reason


def test_xss_dialog_is_a_security_failure_regardless_of_expectation():
    evidence = v.Evidence()
    evidence.dialogs.append({"type": "alert", "message": "XSS", "action": "accept"})
    result = v.evaluate("Input is sanitised and stored as plain text", evidence, "security")
    assert result.status == v.FAIL
    assert "sanitis" in result.reason.lower() or "executed" in result.reason.lower()


def test_stability_expectation_passes_when_nothing_broke():
    """'Handles special characters gracefully' is checkable: no payload dialog,
    no severe console error."""
    evidence = v.Evidence(steps_total=3, steps_executed=3)
    result = v.evaluate(
        "Special characters do not corrupt SQL wildcards or crash grid filtering.",
        evidence, "negative")
    assert result.status == v.PASS
    assert "console error" in result.reason.lower() or "without breaking" in result.reason.lower()


def test_stability_expectation_fails_on_console_errors():
    evidence = v.Evidence(console_errors=["[error] Uncaught TypeError: x is undefined"])
    result = v.evaluate(
        "Search handles SQL special characters safely without exposing database records.",
        evidence, "security")
    assert result.status == v.FAIL


def test_console_warnings_do_not_fail_a_stability_expectation():
    evidence = v.Evidence(console_errors=["[warning] deprecated API used"])
    result = v.evaluate("System safely accepts allowed special characters.", evidence, "boundary")
    assert result.status == v.PASS


def test_common_real_world_phrasings_are_classified():
    samples = {
        "Single character allocation category name is accepted and persisted.": "expects_success",
        "Clearing the search term restores all original records in the grid.": "expects_success",
        "System should reject negative numbers for physical dimension fields.": "expects_error",
        "Read-only or auto-calculated fields are disabled and protected.": "expects_success",
        "Password field should allow typing and mask the entered value.": "expects_success",
        "Grid rows re-order correctly in ascending order on first click.": "expects_success",
        "Data table displays no matching rows message.": "expects_success",
    }
    for text, expected_flag in samples.items():
        intent = v.classify_expectation(text)
        assert intent[expected_flag], f"{text!r} was not classified as {expected_flag}"


def test_download_expectation_passes_when_file_captured():
    evidence = v.Evidence()
    evidence.downloads.append({"suggested_filename": "export.xlsx", "row_count": 120})
    result = v.evaluate("Excel file is downloaded containing all records", evidence, "positive")
    assert result.status == v.PASS


# ──────────────────────────────────────────────────────────────────────
# LLM output repair
# ──────────────────────────────────────────────────────────────────────
def test_expands_repeat_shorthand():
    cleaned = llm.clean_json_text('```json\n[{"value": "A".repeat(5)}]\n```')
    assert '"AAAAA"' in cleaned


def test_expands_multiplier_shorthand():
    assert '"XXX"' in llm.clean_json_text('{"v": "X" * 3}')


def test_trim_for_prompt_keeps_head_and_tail():
    text = "A" * 100 + "ZZZ"
    trimmed = llm.trim_for_prompt(text, limit=50)
    assert trimmed.startswith("A")
    assert trimmed.endswith("ZZZ")
    assert "omitted" in trimmed


# ──────────────────────────────────────────────────────────────────────
# Prerequisite unique data
# ──────────────────────────────────────────────────────────────────────
def test_unique_data_skips_credentials():
    assert executor.make_dep_value_unique("#username", "admin", "T1") == "admin"
    assert executor.make_dep_value_unique("#password", "1234", "T1") == "1234"
    assert executor.make_dep_value_unique("input[name='email']", "a@b.com", "T1") == "a@b.com"


def test_unique_data_suffixes_business_fields():
    assert executor.make_dep_value_unique("#categoryName", "Cotton", "T1") == "CottonT1"


def test_unique_data_skips_numbers_and_dates():
    assert executor.make_dep_value_unique("#qty", "42", "T1") == "42"
    assert executor.make_dep_value_unique("#date", "2026-01-01", "T1") == "2026-01-01"


# ──────────────────────────────────────────────────────────────────────
# Scenario selection
# ──────────────────────────────────────────────────────────────────────
def _suite():
    return [
        {"scenario_name": "create ok", "type": "positive", "steps": []},
        {"scenario_name": "empty name", "type": "negative", "steps": []},
        {"scenario_name": "xss", "type": "security", "steps": []},
        {"scenario_name": "max length", "type": "boundary", "steps": []},
    ]


def test_select_scenarios_assigns_stable_original_ids():
    cfg = executor.RunConfig(project_name="p", flow_name="f", test_types="security")
    chosen = executor.select_scenarios(_suite(), cfg)
    assert len(chosen) == 1
    assert chosen[0]["_original_id"] == 3      # keeps its position in the full suite
    assert chosen[0]["_run_seq"] == 1


def test_select_scenarios_by_case_ids():
    cfg = executor.RunConfig(project_name="p", flow_name="f", case_ids="2,4")
    chosen = executor.select_scenarios(_suite(), cfg)
    assert [c["_original_id"] for c in chosen] == [2, 4]


def test_select_scenarios_limit():
    cfg = executor.RunConfig(project_name="p", flow_name="f", num_cases=2)
    assert len(executor.select_scenarios(_suite(), cfg)) == 2


def test_unmatched_type_filter_falls_back_to_all():
    cfg = executor.RunConfig(project_name="p", flow_name="f", test_types="performance")
    assert len(executor.select_scenarios(_suite(), cfg)) == 4


def test_data_matrix_expansion():
    cfg = executor.RunConfig(project_name="p", flow_name="f", num_cases=1)
    chosen = executor.select_scenarios(_suite(), cfg)
    expanded = executor.expand_data_matrix(chosen, [{"name": "a"}, {"name": "b"}])
    assert len(expanded) == 2
    assert expanded[0]["_data_row"] == {"name": "a"}
    assert expanded[1]["_label"] == "row 2"


# ──────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────
def test_junit_xml_marks_failures_and_skips():
    report = {
        "project_name": "P", "flow_name": "F", "duration_seconds": 12,
        "execution_date": "2026-07-31 10:00:00",
        "passed_count": 1, "failed_count": 1, "unverified_count": 1, "crash_count": 0,
        "checkpoints": [
            {"name": "ok", "status": "PASS", "duration": 3},
            {"name": "bad & <ugly>", "status": "FAIL", "duration": 4,
             "verdict": {"reason": "no validation appeared"}},
            {"name": "meh", "status": "UNVERIFIED", "duration": 5,
             "verdict": {"reason": "nothing proved it"}},
        ],
    }
    xml = executor.build_junit_xml(report)
    assert xml.count("<testcase") == 3
    assert "<failure" in xml and "<skipped" in xml
    assert "&amp;" in xml and "&lt;ugly&gt;" in xml   # escaping


def test_humanize_error_is_readable():
    msg = executor.humanize_error(
        Exception("Timeout 5000ms exceeded waiting for locator('#code')"), "CLICK", "#code")
    assert "could not interact" in msg.lower()
    assert "#code" in msg


def test_dialog_policy_inference():
    assert executor.infer_dialog_policy({
        "scenario_name": "Delete - Cancel Confirmation Dialog",
        "expected_result": "Dialog closes and the record remains"}) == "dismiss"
    assert executor.infer_dialog_policy({
        "scenario_name": "Delete record - confirm",
        "expected_result": "Record is removed"}) == "accept"


# ──────────────────────────────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────────────────────────────
def test_interval_schedule_fires_when_stale():
    entry = {"enabled": True, "trigger": {"type": "interval", "minutes": 30},
             "last_run": "2020-01-01 00:00:00"}
    assert scheduler.is_due(entry) is True


def test_interval_schedule_waits_when_fresh():
    entry = {"enabled": True, "trigger": {"type": "interval", "minutes": 30},
             "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    assert scheduler.is_due(entry) is False


def test_disabled_schedule_never_fires():
    entry = {"enabled": False, "trigger": {"type": "interval", "minutes": 1}}
    assert scheduler.is_due(entry) is False


def test_daily_schedule_fires_once_per_slot():
    now = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0)
    entry = {"enabled": True, "trigger": {"type": "daily", "at": "09:00"}, "last_run": ""}
    assert scheduler.is_due(entry, now) is True
    entry["last_run"] = now.replace(hour=9, minute=1).strftime("%Y-%m-%d %H:%M:%S")
    assert scheduler.is_due(entry, now) is False


def test_assertion_detail_survives_the_humanizer():
    """Regression: a precise assertion message was being rewritten into a generic
    "the element was not found… the field name changed", which sent testers
    hunting for a selector problem that did not exist."""
    raw = ("Validation failed: Key table record matching 'Dinesh' (token: 'Dinesh') "
           "was not found in the UI list.")
    out = executor.humanize_error(raw, "VERIFY_ROW_CONTAINS", "table")
    assert "Dinesh" in out
    assert "field name changed" not in out


def test_disabled_diagnostics_are_not_rewritten():
    """explain_click_failure already writes for a tester; the humanizer must not
    replace its explanation with a generic one."""
    raw = ("CLICK failed on 'button:has-text(\"Save\")' — the control is DISABLED because "
           "these required fields are still empty: M/C Width (inch) *.")
    out = executor.humanize_error(raw, "CLICK", 'button:has-text("Save")')
    assert "M/C Width" in out
    assert "DISABLED" in out


def test_generic_humanising_still_applies_to_raw_playwright_errors():
    out = executor.humanize_error("locator.click: Timeout 5000ms exceeded waiting for locator",
                                  "CLICK", "#save")
    assert "Could not interact with" in out


def test_required_fields_reach_the_generation_prompt():
    """A recording that marks a field required must tell the agents so, or they
    generate a save scenario that can never click a disabled Save button."""
    from app import main as app_main
    context = app_main.build_fields_context([
        {"action": "TYPE", "selector": "#machineName", "tag": "input",
         "label": "M/C (Machine Name) *", "required": True},
        {"action": "TYPE", "selector": 'input[placeholder="Enter width"]', "tag": "input",
         "label": "M/C Width (inch) *", "required": True},
        {"action": "CLICK", "selector": 'button:has-text("Save")', "text": "Save"},
    ])
    assert "[REQUIRED]" in context
    assert "MANDATORY FIELDS" in context
    assert "#machineName" in context and "Enter width" in context
    assert "VERIFY_DISABLED" in context      # tells the agent what to do instead


# ──────────────────────────────────────────────────────────────────────
# Self-healing guardrails — the line the engine must never cross
# ──────────────────────────────────────────────────────────────────────
def test_engine_will_not_fill_the_field_a_scenario_is_testing():
    """Auto-completing a required field is right when the scenario ignores it,
    and catastrophic when the scenario is testing it: filling the name in
    "empty name is rejected" would silently prove the opposite."""
    scenario = {
        "scenario_name": "Verify validation on empty Machine Name",
        "expected_result": "A validation error appears because the Machine Name is empty.",
        "steps": [{"action": "CLICK", "selector": 'button:has-text("Save")'}],
    }
    name_field = {"selector": "#machineName", "label": "M/C (Machine Name) *"}
    width_field = {"selector": 'input[placeholder="Enter width"]', "label": "M/C Width (inch) *"}

    assert executor.scenario_owns_field(scenario, name_field) is True
    assert executor.scenario_owns_field(scenario, width_field) is False


def test_a_field_named_in_the_steps_counts_as_owned():
    scenario = {
        "scenario_name": "Create with valid data",
        "expected_result": "The record is saved successfully.",
        "steps": [{"action": "TYPE", "selector": "#machineName", "value": "X"}],
    }
    assert executor.scenario_owns_field(scenario, {"selector": "#machineName"}) is True
    assert executor.scenario_owns_field(scenario, {"selector": "#machineWidth"}) is False


def test_submit_controls_are_recognised():
    assert executor.looks_like_submit({}, 'button:has-text("Save")') is True
    assert executor.looks_like_submit({}, "#gSaveBtn") is True
    assert executor.looks_like_submit({}, 'button:has-text("Add New")') is True
    assert executor.looks_like_submit({}, "#searchBox") is False


def test_never_invents_a_value_for_a_secret():
    from app import learned
    assert learned.is_sensitive({"label": "Password *"}) is True
    assert learned.is_sensitive({"selector": "#otpCode"}) is True
    assert learned.is_sensitive({"label": "M/C Width (inch) *"}) is False


def test_synthetic_values_respect_the_constraints_the_dom_advertises():
    from app import learned
    assert learned.synthetic_value({"kind": "number", "min": "5", "max": "500"}) == "5"
    assert learned.synthetic_value({"kind": "number", "min": "-10"}) == "1"
    assert len(learned.synthetic_value({"kind": "text", "maxlength": "4"}, "abcdef")) <= 4
    assert learned.synthetic_value({"kind": "select", "options": ["A", "B"]}) == "A"
    assert learned.synthetic_value({"kind": "email"}).endswith("@example.com")


def test_blocked_submission_satisfies_a_negative_expectation():
    """The app refusing to submit IS the validation — a stronger guarantee than
    an error message, because the invalid data never reached the server."""
    evidence = v.Evidence(steps_total=1, steps_executed=1)
    evidence.submission_blocked = {"control": "#saveBtn", "empty_required": ["Item name *"]}
    result = v.evaluate("A validation error prevents the item from being created",
                        evidence, "negative")
    assert result.status == v.PASS
    assert "blocked the submission" in result.reason


def test_blocked_submission_fails_a_positive_expectation():
    evidence = v.Evidence(steps_total=1, steps_executed=1)
    evidence.submission_blocked = {"control": "#saveBtn", "empty_required": ["Width *"]}
    result = v.evaluate("The record is saved successfully and appears in the grid",
                        evidence, "positive")
    assert result.status == v.FAIL
    assert "Width" in result.reason


def test_selectors_are_scoped_to_the_open_dialog():
    """The background-field bug: with a dialog open, an unscoped selector matches
    a look-alike behind it (DOM order puts the background first), so the value
    never reaches the form the tester filled."""
    mark = executor.SURFACE_MARK
    assert executor.scope_to_surface('input[placeholder="Days"]') == \
        f'[{mark}] input[placeholder="Days"]'
    # Every branch of a comma list must be scoped, not just the first.
    assert executor.scope_to_surface("input[readonly], input[disabled]") == \
        f"[{mark}] input[readonly], [{mark}] input[disabled]"
    # Engine-specific syntax is left alone rather than corrupted.
    assert executor.scope_to_surface("xpath=//button") == ""
    assert executor.scope_to_surface("text=Save") == ""
    assert executor.scope_to_surface("p-dialog >> button") == ""
    # Already scoped stays as-is.
    assert executor.scope_to_surface(f"[{mark}] button") == f"[{mark}] button"


# ── Scenarios vs test cases ────────────────────────────────────────────
def test_a_scenario_expands_into_one_test_case_per_data_row():
    """A scenario is the objective; each data row is a test case. Nine values
    for one field used to mean nine hand-written entries repeating the same
    steps — and coverage depended on the model remembering all nine."""
    from app import coverage

    scenario = {
        "scenario_name": "Machine Name validation",
        "type": "negative",
        "steps": [
            {"action": "CLICK", "selector": "#addBtn"},
            {"action": "TYPE", "selector": "#name", "value": "{{value}}"},
            {"action": "CLICK", "selector": "#save"},
        ],
        "expected_result": "handled per the rules",
        "data_table": [
            {"case_name": "empty is rejected", "values": {"value": ""},
             "expected_result": "Required is shown"},
            {"case_name": "255 characters accepted", "values": {"value": "A" * 255},
             "expected_result": "saves", "type": "boundary"},
        ],
    }
    cases = coverage.expand_scenarios([scenario])

    assert len(cases) == 2
    assert cases[0]["scenario_name"] == "Machine Name validation — empty is rejected"
    assert cases[0]["_parent_name"] == "Machine Name validation"
    assert cases[0]["_data_row"] == {"value": ""}
    assert cases[0]["expected_result"] == "Required is shown"
    assert cases[0]["type"] == "negative"          # inherited from the scenario
    assert cases[1]["type"] == "boundary"          # row overrides it
    assert cases[1]["_case_no"] == 2 and cases[1]["_case_total"] == 2
    # The template is not carried into the runnable case.
    assert "data_table" not in cases[0]
    # Steps are intact, so {{value}} resolves from _data_row at run time.
    assert cases[0]["steps"][1]["value"] == "{{value}}"


def test_a_flat_suite_passes_through_untouched():
    """Every suite generated before this existed must run exactly as before."""
    from app import coverage

    flat = [{"scenario_name": "Login works", "steps": [], "expected_result": "in"}]
    assert coverage.expand_scenarios(flat) == flat


def test_the_global_csv_no_longer_multiplies_a_parameterised_scenario():
    """9 enumerated values × 6 CSV rows = 54 runs nobody asked for."""
    from app import executor

    own = {"scenario_name": "Name validation", "_parent_name": "Name validation",
           "_original_id": 1, "steps": []}
    plain = {"scenario_name": "Create machine", "_original_id": 2, "steps": []}
    rows = [{"city": "Pune"}, {"city": "Surat"}]

    out = executor.expand_data_matrix([own, plain], rows)
    names = [c["scenario_name"] for c in out]
    assert names.count("Name validation") == 1      # left alone
    assert names.count("Create machine") == 2       # still data-driven


def test_field_enumeration_is_derived_from_the_field_not_from_judgement():
    """The whole point: coverage you can count. A text field always gets its
    empty / boundary / over-boundary / injection cases, because they are
    enumerated from the field's own type — no model decides."""
    from app import coverage

    rows = coverage.enumerate_field_cases({
        "selector": "#machineName", "label": "Machine Name *",
        "kind": "text", "required": True, "max_length": 100,
    })
    names = [r["case_name"] for r in rows]

    for expected in ("empty is rejected", "whitespace only is rejected",
                     "single character is accepted",
                     "maximum length (100) is accepted",
                     "one over the maximum (101) is refused",
                     "leading and trailing spaces are trimmed",
                     "unicode and emoji are handled",
                     "SQL injection is stored as text",
                     "HTML and script injection is escaped"):
        assert expected in names, f"missing {expected!r}"

    by_name = {r["case_name"]: r for r in rows}
    assert len(by_name["maximum length (100) is accepted"]["values"]["value"]) == 100
    assert len(by_name["one over the maximum (101) is refused"]["values"]["value"]) == 101
    assert by_name["SQL injection is stored as text"]["type"] == "security"


def test_number_enumeration_covers_the_two_real_bugs_we_found():
    """Circular Machine accepted a negative Production Capacity and leaked
    'Numeric value out of range of int' to the user. Both are now enumerated
    for every numeric field, so no suite can omit them."""
    from app import coverage

    rows = coverage.enumerate_field_cases({
        "selector": "#capacity", "label": "Production Capacity (m/day)",
        "kind": "number", "required": False,
    })
    by_name = {r["case_name"]: r for r in rows}

    assert by_name["negative value is rejected"]["values"]["value"] == "-1"
    assert "defect" in by_name["negative value is rejected"]["expected_result"]

    overflow = by_name["numeric overflow is refused gracefully"]
    assert overflow["values"]["value"] == "1" + "0" * 18
    assert "Numeric value out of range" in overflow["expected_result"]

    assert by_name["non-numeric text is rejected"]["values"]["value"] == "abcd"


def test_a_dropdown_enumerates_its_options_and_a_password_is_never_fuzzed():
    from app import coverage

    choices = coverage.enumerate_field_cases({
        "selector": "#status", "label": "Status *", "kind": "dropdown",
        "required": True, "options": ["Active", "Inactive"],
    })
    names = [c["case_name"] for c in choices]
    assert "no selection is rejected" in names
    assert "'Active' can be selected" in names
    assert "'Inactive' can be selected" in names

    # Fuzzing a password locks the account and every later scenario fails for a
    # reason that has nothing to do with it.
    assert coverage.enumerate_field_cases(
        {"selector": "#password", "label": "Password *", "kind": "password",
         "required": True}) == []
    assert coverage.enumerate_field_cases(
        {"selector": "#otpCode", "label": "OTP", "kind": "text"}) == []


def test_results_group_back_under_their_scenario():
    """26 failures that are really 3 defects hit with different data should read
    as 3 findings."""
    from app import coverage

    checkpoints = [
        {"name": "Name validation — empty", "parent": "Name validation",
         "case": "empty", "status": "PASS"},
        {"name": "Name validation — 256 chars", "parent": "Name validation",
         "case": "256 chars", "status": "FAIL"},
        {"name": "Name validation — emoji", "parent": "Name validation",
         "case": "emoji", "status": "FAIL"},
        {"name": "Export the list", "parent": "", "status": "PASS"},
    ]
    groups = coverage.group_results(checkpoints)

    assert len(groups) == 2
    first = groups[0]
    assert first["scenario"] == "Name validation"
    assert first["summary"] == "1 of 3 test cases passed"
    assert first["verdict"] == "FAIL"
    assert first["failures"] == ["256 chars", "emoji"]
    # A flat case still forms its own group, so the old presentation survives.
    assert groups[1]["scenario"] == "Export the list"
    assert groups[1]["summary"] == "1 of 1 test cases passed"


def test_the_checklist_names_the_behaviours_a_suite_never_touched():
    """This is what would have caught the missing audit-history coverage on
    Circular Machine before the suite was ever run."""
    from app import coverage

    suite = [
        {"scenario_name": "Create Circular Machine with all valid fields",
         "steps": [], "expected_result": "saved"},
        {"scenario_name": "Search machine by exact name", "steps": [],
         "expected_result": "found"},
    ]
    gaps = {g["id"] for g in coverage.checklist_gaps(suite)}

    assert "audit" in gaps           # the bug that was never raised
    assert "export" in gaps
    assert "delete" in gaps
    assert "readonly" in gaps
    assert "create_all" not in gaps  # it is covered
    assert "search_exact" not in gaps

    summary = coverage.coverage_summary(suite)
    assert summary["test_cases"] == 2
    assert summary["checklist_total"] == len(coverage.SCENARIO_CHECKLIST)
    assert 0 < summary["checklist_percent"] < 100


def test_a_checklist_word_cannot_hide_inside_another_word():
    """"log" must not match *login* or *dialog*. A plain substring test reported
    audit-trail coverage for a suite that only had login scenarios — the same
    mistake that once let a failed click on #taxCode fall through to Cancel."""
    from app import coverage

    login_only = [
        {"scenario_name": "Login with valid credentials", "steps": [],
         "expected_result": "the dashboard loads"},
        {"scenario_name": "Close the dialog without saving", "steps": [],
         "expected_result": "nothing is saved"},
    ]
    gaps = {g["id"] for g in coverage.checklist_gaps(login_only)}
    assert "audit" in gaps, "login/dialog must not count as audit-trail coverage"

    real_audit = login_only + [
        {"scenario_name": "Audit history shows date and user", "steps": [],
         "expected_result": "every row has a timestamp"},
    ]
    assert "audit" not in {g["id"] for g in coverage.checklist_gaps(real_audit)}

    # And the word still matches with punctuation or casing around it.
    assert coverage._mentions("open the audit-trail dialog", "audit")
    assert coverage._mentions("check history, then close", "history")
    assert not coverage._mentions("prehistory of the module", "history")


# ── §2: never convert ignorance into a FAIL ────────────────────────────
def test_unknown_message_surface_yields_unverified_not_fail(monkeypatch):
    """A false FAIL is the most damaging thing this tool can produce: it blames a
    working application. When the engine has never seen where this app renders
    messages, "nothing appeared" and "something appeared I cannot read" are
    indistinguishable, so the honest answer is UNVERIFIED."""
    from app import config, verdict

    ev = verdict.Evidence(steps_total=4, steps_executed=4)
    expectation = "A validation error should appear because the field is required."

    # Default behaviour is unchanged — existing verdict numbers do not move.
    assert config.STRICT_MESSAGE_CONFIDENCE is False
    assert verdict.evaluate(expectation, ev, "negative",
                            message_surface_known=False).status == verdict.FAIL

    monkeypatch.setattr(config, "STRICT_MESSAGE_CONFIDENCE", True)
    unknown = verdict.evaluate(expectation, ev, "negative", message_surface_known=False)
    assert unknown.status == verdict.UNVERIFIED
    assert "never been observed" in unknown.reason

    # Once the surface IS known, silence is real evidence and FAIL is correct.
    known = verdict.evaluate(expectation, ev, "negative", message_surface_known=True)
    assert known.status == verdict.FAIL


def test_more_success_wordings_are_recognised():
    """"was accepted" is a success confirmation. Classifying it "unknown" left
    scenarios UNVERIFIED that had visible proof on screen."""
    from app import verdict

    for text in ("Reference REF-1 was accepted", "Record approved",
                 "Machine activated", "File uploaded"):
        assert verdict.classify_message(text, "div.zx-note-9") == "success", text

    # Negation must not flip to success.
    assert verdict.classify_message("Machine not accepted - invalid width",
                                    "div.zx-note-9") != "success"
    assert verdict.classify_message("Upload failed", "div.zx-note-9") == "error"


# ── A guessed message literal must not fail a working application ──────
def test_a_wording_mismatch_is_unverified_not_failed():
    """The real case: the app said 'Invalid Password', the generated step
    demanded 'Invalid credentials', and the scenario was reported FAIL — blaming
    a working application for wording a model invented."""
    from app import verdict

    ev = verdict.Evidence(steps_total=5, steps_executed=5)
    ev.add_message("Invalid Password", "div.p-toast-detail")
    ev.wording_mismatches = [{
        "expected": "Invalid credentials",
        "actual": ["Invalid Password", "ERROR"],
        "selector": "div.p-toast-detail", "step": 5,
    }]
    result = verdict.evaluate(
        "Authentication fails, error message is displayed, and user remains on "
        "login screen.", ev, "negative")

    assert result.status == verdict.UNVERIFIED
    assert "Invalid credentials" in result.reason      # what the step wanted
    assert "Invalid Password" in result.reason         # what the app said
    assert "update the step" in result.reason.lower()


def test_a_real_missing_message_still_fails():
    """The safety check: when nothing appeared at all there is no mismatch to
    record, so a genuine miss is still a failure."""
    from app import verdict

    ev = verdict.Evidence(steps_total=5, steps_executed=5)
    assert not ev.wording_mismatches
    result = verdict.evaluate(
        "Validation error triggers and submission is blocked.", ev, "negative")
    assert result.status == verdict.FAIL


def test_a_matching_message_still_passes_normally():
    from app import verdict

    ev = verdict.Evidence(steps_total=5, steps_executed=5)
    ev.add_message("Invalid Password", "div.p-toast-detail")
    result = verdict.evaluate(
        "Authentication fails and an error message is displayed.", ev, "negative")
    assert result.status == verdict.PASS
