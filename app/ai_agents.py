import json
import re

# Shared vocabulary appended to every generation prompt so the agents only emit
# verbs the execution engine can actually run, and always leave a provable
# assertion behind (this is what stops "green but unverified" test suites).
STEP_VOCABULARY = """
    SUPPORTED STEP ACTIONS (use ONLY these):
    - "NAVIGATE"  : {"action": "NAVIGATE", "selector": "https://full/url/or/path"}
    - "CLICK"     : {"action": "CLICK", "selector": "CSS selector"}
    - "TYPE"      : {"action": "TYPE", "selector": "CSS selector", "value": "text"}
    - "CLEAR"     : {"action": "CLEAR", "selector": "CSS selector"}
    - "SELECT"    : {"action": "SELECT", "selector": "dropdown selector", "value": "Option label"}
    - "CHECK" / "UNCHECK" : {"action": "CHECK", "selector": "checkbox selector"}
    - "KEYPRESS"  : {"action": "KEYPRESS", "selector": "optional selector", "value": "Enter"}
    - "UPLOAD"    : {"action": "UPLOAD", "selector": "input[type=file]", "value": "file.csv"}
    - "HOVER", "SCROLL_TO", "WAIT" (value = milliseconds)
    - "DIALOG"    : {"action": "DIALOG", "value": "accept" | "dismiss"}  (native confirm boxes)

    ASSERTION ACTIONS — every scenario MUST end with at least one of these:
    - "VERIFY_MESSAGE"     : validation/toast message appeared. value = expected text (optional).
    - "VERIFY_ROW_CONTAINS": {"selector": "table", "value": "Record name"} record is listed.
    - "VERIFY_ROW_ABSENT"  : {"selector": "table", "value": "Record name"} record is NOT listed.
    - "VERIFY_VALUE"       : field holds an exact value.
    - "VERIFY_TEXT"        : {"selector": "css", "value": "expected text"} element shows text.
    - "VERIFY_DISABLED", "VERIFY_ENABLED", "VERIFY_READONLY", "VERIFY_NON_ZERO"
    - "VERIFY_VISIBLE", "VERIFY_HIDDEN", "VERIFY_URL", "VERIFY_COUNT", "VERIFY_DOWNLOAD"
    - "VERIFY_OPTION_PRESENT" / "VERIFY_OPTION_ABSENT" : dropdown membership
          {"action": "VERIFY_OPTION_ABSENT", "selector": "#categoryDropdown", "value": "Cotton"}
    - "VERIFY_COLUMN_NOT_EMPTY" : columns that must always carry data
          {"action": "VERIFY_COLUMN_NOT_EMPTY", "selector": "#auditTable",
           "value": "Date/Time, User"}
    - "VERIFY_NO_NOOP_ROWS" : history rows that record a change that never happened
          {"action": "VERIFY_NO_NOOP_ROWS", "selector": "#auditTable",
           "value": "Old Value, New Value"}
    - "STORE" : capture a value the application generated, for use later
          {"action": "STORE", "selector": "#customerCode", "value": "customer_code"}

    MANDATORY ASSERTION RULES:
    - A negative / boundary / security scenario MUST end with "VERIFY_MESSAGE" (the
      validation the app is expected to show) and, where relevant,
      "VERIFY_ROW_ABSENT" to prove the bad record was not saved.
    - A create / update scenario MUST end with "VERIFY_ROW_CONTAINS" for the record.
    - ASSERT ONLY ON DATA THIS SCENARIO CREATED. Never assert on a value that came
      from the recording (an old machine name, a code someone typed once). That
      record may not exist any more, and the failure would say nothing about the
      application. Use the value this scenario itself typed, or {{run_token}} to
      make it unique, and verify that.
    - A row action column is usually four unlabelled icon buttons: view, edit,
      history/audit and delete. Generate a scenario for EACH of them, using the
      icon-based selector from the recording (e.g. button:has(i.pi-history)),
      never a positional one such as td:nth-of-type(12) > button.
    - For a history / audit dialog, asserting that rows exist is not enough. Add
      "VERIFY_COLUMN_NOT_EMPTY" with the columns that must always be populated
      (typically "Date/Time, User") and "VERIFY_NO_NOOP_ROWS" for Old/New value
      pairs, because a trail with blank timestamps or entries recording no real
      change is useless for traceability even though every row is present.
    - A delete scenario MUST end with "VERIFY_ROW_ABSENT"; a cancel-delete scenario
      MUST end with "VERIFY_ROW_CONTAINS" (the record must survive) and include
      {"action": "DIALOG", "value": "dismiss"} before the delete click if the app
      uses a native confirm box.
    - An export scenario MUST end with "VERIFY_DOWNLOAD".
    - Never use an action that is not in the list above.
"""


def _clean_json_response(raw_text: str) -> str:
    """Strip markdown wrappers and repair LLM shorthand from Gemini responses."""
    raw_text = (raw_text or "").strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw_text = "\n".join(lines).strip()
    raw_text = re.sub(r'"([^"]*)"\.repeat\((\d+)\)',
                      lambda m: '"' + (m.group(1) * min(int(m.group(2)), 5000)) + '"', raw_text)
    raw_text = re.sub(r'"([^"]*)"\s*\*\s*(\d+)',
                      lambda m: '"' + (m.group(1) * min(int(m.group(2)), 5000)) + '"', raw_text)
    return raw_text


def _build_agent1_prompt(flow_name: str, target_url: str, actions_json: str, fields_context: str, missing_scenarios: list = None, test_types: str = "", rules_block: str = "") -> str:
    """Build Agent 1 (Functional Generator) prompt."""
    missing_instruction = ""
    if missing_scenarios:
        missing_instruction = f"""
        IMPORTANT: A previous review found MISSING scenarios. You MUST generate test cases SPECIFICALLY for these gaps:
        {json.dumps(missing_scenarios, indent=2)}
        Generate ONLY the missing scenarios listed above. Do NOT regenerate scenarios that already exist.
        """
    
    type_filter_instruction = ""
    if test_types.strip():
        type_filter_instruction = f"\n    USER SELECTED TEST TYPE FILTER: Focus generation specifically on these categories: {test_types}\n"
    
    rules_instruction = ""
    if rules_block.strip():
        rules_instruction = chr(10) + rules_block.strip() + chr(10)

    return f"""
    You are Agent 1: The Functional Test Case Generator (The Builder).
    Your ONLY job is to generate POSITIVE, CRUD, WORKFLOW, FIELD VALIDATION, and UI FUNCTIONAL test cases.
    {type_filter_instruction}{rules_instruction}
    DO NOT generate:
    - Security tests (XSS, SQL injection, CSRF)
    - Boundary value tests (max length, min length)
    - Negative edge case tests (special characters, unicode)
    - Performance or load tests
    
    You MUST generate test cases for these categories:
    - Create with valid data (happy path, all fields, minimum required fields)
    - Dependent Dropdown Selections: Selecting valid active options from prerequisite master dropdowns (e.g. Qualification, Skill, Department, Category)
    - Read/View records (view details modal, field mapping verification)
    - Update/Edit records (modify fields, save changes)
    - Delete records (confirm delete, cancel delete)
    - Search/Filter (exact match, partial match, case-insensitive)
    - Sort columns (ascending/descending for each sortable column)
    - Export functionality (Excel, PDF if available)
    - Dialog interactions (open/close, cancel, form reset)
    - Navigation (menu clicks, page transitions)
    
    DATA PERSISTENCE & TABLE ROW VERIFICATION:
    - Whenever a record is created or updated, generate a verification assertion step to check that the newly created record/customer name actually appears in the UI data table:
      * Use action "VERIFY_ROW_CONTAINS" with "selector": "table" and "value": "Expected Record Name/Code" (or string step "VERIFY ROW CONTAINS Expected Record Name").
    
    FORMULA & DISABLED FIELDS HANDLING:
    - If a field is disabled, read-only, or auto-populated via a formula (e.g. fields containing '(Auto)', 'readonly', or 'disabled'), DO NOT generate a 'TYPE' or 'CLICK' action on it.
    - Instead, generate assertion steps to check them:
      * Use action "VERIFY_DISABLED" (or string step "VERIFY ON selector is disabled") to check that the field is locked.
      * Use action "VERIFY_NON_ZERO" (or string step "VERIFY ON selector value > 0") to check that the formula output is valid and calculated.
      * Use action "VERIFY_VALUE" with "value" (or string step "VERIFY ON selector has value expected_value") to verify exact calculations.
    
    Flow Name: {flow_name}
    Target URL: {target_url}
    Recorded Actions:
    {actions_json}
    
    Fields & Context:
    {fields_context}
    
    {missing_instruction}
    

    SCENARIOS AND THEIR TEST CASES — use the two-level shape:
    A scenario is the objective. Its test cases are the concrete data rows that
    cover it. When several cases differ ONLY by the value put in one field,
    write ONE scenario with a "data_table" instead of repeating the steps:

        {{
            "scenario_name": "Machine Name accepts and rejects the right values",
            "type": "negative",
            "field_under_test": "#machineName",
            "steps": [
                {{"action": "CLICK", "selector": "#addBtn"}},
                {{"action": "TYPE", "selector": "#machineName", "value": "{{{{value}}}}"}},
                {{"action": "TYPE", "selector": "#machineWidth", "value": "72"}},
                {{"action": "CLICK", "selector": "button:has-text('Save')"}}
            ],
            "expected_result": "Each value is handled according to the field's rules.",
            "data_table": [
                {{"case_name": "empty is rejected", "values": {{"value": ""}},
                 "expected_result": "A required-field validation message appears"}},
                {{"case_name": "255 characters accepted", "values": {{"value": "AAA...255"}},
                 "expected_result": "The record saves", "type": "boundary"}}
            ]
        }}

    Rules for data tables:
    - The steps are a TEMPLATE. Refer to the varying value as {{{{value}}}}; every row
      supplies it under "values". Fill every OTHER mandatory field with a valid
      literal, so each row fails only for its own reason.
    - Every row needs its own "expected_result" — that is what is machine-verified.
    - Give each row a short "case_name"; it names the test case in the report.
    - Use a flat scenario (no data_table) when the steps genuinely differ:
      create, edit, delete, search, export, audit history.

    CRITICAL RULES:
    - DO NOT generate login/authentication steps. Assume already logged in.
    - Each test case MUST start with navigation steps to reach the target module page.
    - Use the EXACT selectors from the recorded actions wherever possible.
    - For dropdowns populated by master tables, use realistic active option names.
    - Generate between 15-25 test cases (or as many as needed to cover all functional scenarios).
    - Write the "expected_result" as a single checkable sentence naming the exact
      message, record or state to look for (it is machine-verified after the run).
{STEP_VOCABULARY}

    Output ONLY a valid JSON array of test case objects in this format:
    [
        {{
            "scenario_name": "Descriptive scenario name",
            "type": "positive" | "positive_ui" | "positive_boundary",
            "steps": [
                {{"action": "CLICK", "selector": "CSS selector"}},
                {{"action": "TYPE", "selector": "CSS selector", "value": "input value"}},
                {{"action": "SELECT", "selector": "CSS selector", "value": "Option label"}},
                {{"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": "Record name"}}
            ],
            "expected_result": "What should happen after execution",
            "field_under_test": "optional — the selector this scenario varies",
            "data_table": [
                {{"case_name": "short name of the test case",
                  "values": {{"value": "the concrete input"}},
                  "expected_result": "what must happen for THIS row",
                  "type": "optional per-row override"}}
            ]
        }}
    ]
    """.replace("{STEP_VOCABULARY}", STEP_VOCABULARY)


def _build_agent2_prompt(flow_name: str, target_url: str, actions_json: str, fields_context: str, missing_scenarios: list = None, test_types: str = "", rules_block: str = "") -> str:
    """Build Agent 2 (Edge Case Generator) prompt."""
    missing_instruction = ""
    if missing_scenarios:
        missing_instruction = f"""
        IMPORTANT: A previous review found MISSING scenarios. You MUST generate test cases SPECIFICALLY for these gaps:
        {json.dumps(missing_scenarios, indent=2)}
        Generate ONLY the missing scenarios listed above. Do NOT regenerate scenarios that already exist.
        """
        
    type_filter_instruction = ""
    if test_types.strip():
        type_filter_instruction = f"\n    USER SELECTED TEST TYPE FILTER: Focus generation specifically on these categories: {test_types}\n"
    
    rules_instruction = ""
    if rules_block.strip():
        rules_instruction = chr(10) + rules_block.strip() + chr(10)

    return f"""
    You are Agent 2: The Edge Case & Security Test Generator (The Breaker).
    Your ONLY job is to generate BOUNDARY, NEGATIVE, SECURITY, INACTIVE EXCLUSION, and ERROR HANDLING test cases.
    {type_filter_instruction}{rules_instruction}
    
    DO NOT generate:
    - Basic CRUD happy-path scenarios (create with valid data, simple view, simple edit)
    - Normal search or sort tests
    - Standard dialog open/close tests
    
    You MUST generate test cases for these categories:
    - Inactive Master Exclusion: Verify that items marked 'Status = Inactive' (or soft-deleted) in prerequisite master tables (e.g. Inactive Qualification, Inactive Skill, Inactive Department) are EXCLUDED and hidden from dependent selection dropdowns (expected result: item is not listed as a selectable option).
    - Boundary values: Input at max character limit, input exceeding max limit, single character input
    - Negative: Missing required fields, empty submissions, duplicate unique keys
    - Whitespace: Leading/trailing spaces, whitespace-only input, tab characters
    - Special characters: @#$%^&*() in input fields, HTML tags, emoji characters
    - Security: XSS payload (<script>alert('XSS')</script>), SQL injection (' OR 1=1 --), HTML injection
    - Error handling: Network failure graceful handling, timeout behavior, concurrent edits
    - Data validation: Wrong data types (letters in numeric fields), format mismatches (email, phone)
    - Read-only enforcement: Attempt to edit non-editable/disabled fields
    - Overflow: Very long input strings (200+ chars), very large numeric values
    
    FORMULA & DISABLED FIELDS HANDLING:
    - If a field is disabled, read-only, or auto-populated via a formula (e.g. fields containing '(Auto)', 'readonly', or 'disabled'), DO NOT generate a 'TYPE' or 'CLICK' action on it.
    - Instead, generate assertion steps to check them:
      * Use action "VERIFY_DISABLED" (or string step "VERIFY ON selector is disabled") to check that the field is locked.
      * Use action "VERIFY_NON_ZERO" (or string step "VERIFY ON selector value > 0") to check that the formula output is valid and calculated.
      * Use action "VERIFY_VALUE" with "value" (or string step "VERIFY ON selector has value expected_value") to verify exact calculations.
    
    Flow Name: {flow_name}
    Target URL: {target_url}
    Recorded Actions:
    {actions_json}
    
    Fields & Context:
    {fields_context}
    
    {missing_instruction}
    

    SCENARIOS AND THEIR TEST CASES — use the two-level shape:
    A scenario is the objective. Its test cases are the concrete data rows that
    cover it. When several cases differ ONLY by the value put in one field,
    write ONE scenario with a "data_table" instead of repeating the steps:

        {{
            "scenario_name": "Machine Name accepts and rejects the right values",
            "type": "negative",
            "field_under_test": "#machineName",
            "steps": [
                {{"action": "CLICK", "selector": "#addBtn"}},
                {{"action": "TYPE", "selector": "#machineName", "value": "{{{{value}}}}"}},
                {{"action": "TYPE", "selector": "#machineWidth", "value": "72"}},
                {{"action": "CLICK", "selector": "button:has-text('Save')"}}
            ],
            "expected_result": "Each value is handled according to the field's rules.",
            "data_table": [
                {{"case_name": "empty is rejected", "values": {{"value": ""}},
                 "expected_result": "A required-field validation message appears"}},
                {{"case_name": "255 characters accepted", "values": {{"value": "AAA...255"}},
                 "expected_result": "The record saves", "type": "boundary"}}
            ]
        }}

    Rules for data tables:
    - The steps are a TEMPLATE. Refer to the varying value as {{{{value}}}}; every row
      supplies it under "values". Fill every OTHER mandatory field with a valid
      literal, so each row fails only for its own reason.
    - Every row needs its own "expected_result" — that is what is machine-verified.
    - Give each row a short "case_name"; it names the test case in the report.
    - Use a flat scenario (no data_table) when the steps genuinely differ:
      create, edit, delete, search, export, audit history.

    CRITICAL RULES:
    - DO NOT generate login/authentication steps. Assume already logged in.
    - Each test case MUST start with navigation steps to reach the target module page.
    - Use the EXACT selectors from the recorded actions wherever possible.
    - Generate between 15-25 test cases (or as many as needed to cover all edge cases).
    - For boundary tests, write out full strings literally (e.g., "AAAAAA..." not "A".repeat(50)).
    - The "expected_result" MUST name the exact validation message or state expected,
      because it is machine-verified against the live screen after execution.
{STEP_VOCABULARY}

    Output ONLY a valid JSON array of test case objects in this format:
    [
        {{
            "scenario_name": "Descriptive scenario name",
            "type": "negative" | "boundary" | "negative_boundary" | "security" | "positive_boundary",
            "steps": [
                {{"action": "CLICK", "selector": "CSS selector"}},
                {{"action": "TYPE", "selector": "CSS selector", "value": "input value"}},
                {{"action": "CLICK", "selector": "button:has-text(\\"Save\\")"}},
                {{"action": "VERIFY_MESSAGE", "value": "Name is required"}},
                {{"action": "VERIFY_ROW_ABSENT", "selector": "table", "value": "input value"}}
            ],
            "expected_result": "What should happen after execution",
            "field_under_test": "optional — the selector this scenario varies",
            "data_table": [
                {{"case_name": "short name of the test case",
                  "values": {{"value": "the concrete input"}},
                  "expected_result": "what must happen for THIS row",
                  "type": "optional per-row override"}}
            ]
        }}
    ]
    """.replace("{STEP_VOCABULARY}", STEP_VOCABULARY)


def _build_agent3_prompt(merged_cases: list, flow_name: str, fields_context: str,
                         rules_block: str = "") -> str:
    """Build Agent 3 (QA Reviewer) prompt.

    The checklist is computed before the model is asked anything: word-matching
    over the merged suite is deterministic, so the gaps it reports are facts the
    reviewer is told, not conclusions it has to reach. It can still find gaps of
    its own — it just cannot miss these.
    """
    from . import coverage as coverage_engine

    computed = coverage_engine.checklist_gaps(merged_cases)
    summary = coverage_engine.coverage_summary(merged_cases)
    measured = (
        f"{chr(10)}    MEASURED COVERAGE (computed from the suite, not your opinion):{chr(10)}"
        f"    {summary['test_cases']} test cases across {summary['scenarios']} scenarios; "
        f"{summary['checklist_covered']}/{summary['checklist_total']} checklist items "
        f"({summary['checklist_percent']}%).{chr(10)}"
    )
    if computed:
        measured += ("    These behaviours have NO matching test case and MUST appear in your "
                     f"missing list:{chr(10)}")
        for gap in computed:
            measured += f"      - {gap['id']}: {gap['title']} ({gap['type']}){chr(10)}"
    else:
        measured += (f"    Every checklist item is matched. Look for gaps the checklist does "
                     f"not cover: data permutations, role differences, dependent masters.{chr(10)}")

    rules_instruction = measured
    if rules_block.strip():
        rules_instruction += (chr(10) + rules_block.strip() + chr(10) +
                              "    Treat any rule with no covering scenario as a coverage gap." + chr(10))
    return f"""
    You are Agent 3: The QA Coverage Reviewer & Auditor.{rules_instruction}
    Your job is to REVIEW a merged test suite and identify MISSING scenarios.
    
    You must NOT generate test cases yourself. You ONLY analyze and report gaps.
    
    Review the following merged test suite against this COVERAGE MATRIX:
    
    COVERAGE MATRIX (every item must have at least 1 matching test case):
    {{
        "create_happy_path": "At least 1 create with all valid fields",
        "create_minimum_required": "Create with only required fields",
        "dependent_dropdown_selection": "Select valid active master option in relational dropdowns",
        "read_view_record": "View/read a single record details",
        "update_edit_record": "Edit and save changes to a record",
        "delete_confirm": "Delete with confirmation (Yes, Delete)",
        "delete_cancel": "Cancel delete action",
        "search_exact_match": "Search by exact value",
        "search_case_insensitive": "Case-insensitive search",
        "search_no_results": "Search returning no matches",
        "sort_ascending_descending": "Sort by at least 2 columns",
        "required_field_missing": "Submit with missing required field",
        "duplicate_unique_key": "Submit duplicate unique key value",
        "inactive_master_exclusion": "Verify items marked Inactive in master tables are hidden from dependent selection dropdowns",
        "max_length_at_limit": "Input at maximum character limit",
        "max_length_over_limit": "Input exceeding maximum character limit",
        "special_characters_input": "Input with special characters (@#$%)",
        "whitespace_only_input": "Input with only spaces",
        "leading_trailing_spaces": "Input with leading/trailing whitespace",
        "xss_injection": "XSS payload in input field",
        "sql_injection": "SQL injection string in input field",
        "cancel_dialog": "Cancel/close dialog without saving",
        "export_data": "Export/download functionality (Excel/PDF)",
        "empty_optional_fields": "Submit with empty optional fields",
        "readonly_field_check": "Verify read-only fields cannot be edited"
    }}
    
    Flow Name: {flow_name}
    Fields & Context: {fields_context}
    
    MERGED TEST SUITE TO REVIEW:
    {json.dumps(merged_cases, indent=2)}
    
    For each coverage matrix item, check if there is AT LEAST one matching test case in the suite.
    
    Output ONLY a valid JSON object in this format:
    {{
        "coverage_complete": true/false,
        "coverage_percentage": 85,
        "covered_items": ["create_happy_path", "delete_confirm", ...],
        "missing_items": [
            {{
                "matrix_key": "xss_injection",
                "description": "No test case found for XSS payload injection in any input field",
                "suggested_type": "security",
                "priority": "high"
            }}
        ],
        "duplicate_scenarios": ["List of scenario names that appear duplicated"],
        "review_notes": "Overall assessment of the test suite quality"
    }}
    """


def build_spec_prompt(flow_name: str, target_url: str, actions_json: str,
                      generation_instruction: str) -> str:
    """Single-agent enterprise QA specification prompt (used by /analyze-flow)."""
    return f"""
    You are a staff QA Test Architect, Principal Quality Engineer, and Enterprise Automation Expert.
    Analyze this recorded flow of user actions on a webpage:
    Flow Name: {flow_name}
    Target URL: {target_url}
    Recorded Actions:
    {actions_json}

    Perform a comprehensive QA analysis strictly following the 15-step reasoning process of an
    Enterprise QA Architect:
    1. Understand business domain.      2. Understand user's goal.
    3. Understand feature.              4. Understand business rules.
    5. Understand user roles.           6. Understand permissions.
    7. Understand dependencies.         8. Understand integrations.
    9. Understand database impact.      10. Understand API impact.
    11. Understand security impact.     12. Understand performance impact.
    13. Understand automation feasibility. 14. Understand regression impact.
    15. Generate complete QA recommendations.

    Additional Guidelines for this Specific Flow:
    {generation_instruction}
{STEP_VOCABULARY}

    You must output strictly in JSON format matching this schema.
    CRITICAL: All JSON keys and values must be standard, raw JSON literals. Do NOT use string
    repeat methods or programmatic multipliers (like '"P".repeat(255)' or '"N" * 100') to
    represent boundary data. Write out string literals (e.g. 'PPPP...') fully inside the values.
    {{
        "requirement_understanding": "Detailed statement of business domain and functional objectives.",
        "requirement_gaps": ["Identified requirement gaps, underspecified rules, missing workflows."],
        "business_rules": ["Core business rules and validations observed or required."],
        "assumptions": ["Operational assumptions, data preconditions, environment setups."],
        "risks": ["High-risk areas, potential data loss conditions, critical release risks."],
        "modules_impacted": ["Other parts of the application or database impacted."],
        "test_strategy": {{
            "objectives": "Core testing objectives",
            "scope_in": "What is in scope",
            "scope_out": "What is out of scope",
            "rollback_plan": "Post-release rollback strategy"
        }},
        "test_scenarios": ["Core test scenarios mapped to CRUD objectives"],
        "test_cases": [
            {{
                "scenario_name": "Detailed scenario name",
                "type": "positive" | "negative" | "boundary" | "security" | "ui",
                "steps": [
                    {{"action": "TYPE", "selector": "CSS selector", "value": "Value"}},
                    {{"action": "CLICK", "selector": "CSS selector"}},
                    {{"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": "Value"}}
                ],
                "expected_result": "Expected validation message, page state, or redirect"
            }}
        ],
        "test_data": {{
            "valid": [{{ "key": "value" }}],
            "invalid": [{{ "key": "value" }}],
            "boundary": [{{ "key": "value" }}]
        }},
        "ui_validation": ["Visual checklist (alignment, contrast, dark-mode, tab-order)"],
        "api_validation": ["API contract verifications, schema checks, response code mappings"],
        "database_validation": ["State updates, foreign keys, index locks, history inserts"],
        "security_validation": ["OWASP Top 10 checks: CSRF, SQLi, XSS, rate limits, cookies"],
        "performance_validation": ["Load metrics, latency targets, caching rules, stress limits"],
        "accessibility_validation": ["Contrast, aria attributes, focus outlines, tab order"],
        "automation_candidates": ["Parts suitable for CI/CD Playwright scheduling"],
        "regression_impact": ["Critical regressions, shared libraries, login hooks affected"],
        "defect_prevention_suggestions": ["Code fixes, selector improvements, constraints"],
        "release_readiness": "Build recommendation (READY with caveats / NOT READY due to gaps)",
        "coverage_summary": "Requirement mapping summary coverage percentage assessment",
        "ai_self_review": "Did I miss any validation, business rule, or database check?",
        "suggestions": {{
            "broken_selectors": ["potential selector improvements"],
            "missing_validations": ["unhandled edge cases"],
            "recommendations": ["general recommendations"]
        }},
        "scripts": {{
            "playwright_python": "Playwright python script starting at the target page",
            "cypress_js": "Cypress script code block",
            "selenium_java": "Selenium Java code block"
        }},
        "documentation_markdown": "Markdown documentation summarizing the flow step by step"
    }}
    """.replace("{STEP_VOCABULARY}", STEP_VOCABULARY)


def build_selector_repair_prompt(flow_name: str, missing: list, dom_summary: str) -> str:
    """Ask the model to map hallucinated selectors onto real DOM elements."""
    return f"""
    You are a Playwright locator repair specialist.
    Flow: {flow_name}

    These selectors were generated for automated tests but DO NOT exist on the live page:
    {json.dumps(missing, indent=2)}

    Here are the interactive elements that DO exist on the page right now:
    {dom_summary}

    For each broken selector, choose the best real replacement from the list above, matching by
    label, placeholder, name, formcontrolname, aria-label or button text. If nothing plausibly
    matches, return an empty string for that selector so a human can review it.

    Output ONLY this JSON:
    {{"repairs": [{{"broken": "original selector", "replacement": "real selector or empty",
                    "confidence": 0.0-1.0, "reason": "why this matches"}}]}}
    """
