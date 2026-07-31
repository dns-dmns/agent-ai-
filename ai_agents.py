import json
import re

def _clean_json_response(raw_text: str) -> str:
    """Strip markdown wrappers and repair LLM shorthand from Gemini responses."""
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw_text = "\n".join(lines).strip()
    raw_text = re.sub(r'"([^"]+)"\.repeat\((\d+)\)', lambda m: '"' + (m.group(1) * int(m.group(2))) + '"', raw_text)
    raw_text = re.sub(r'"([^"]+)"\s*\*\s*(\d+)', lambda m: '"' + (m.group(1) * int(m.group(2))) + '"', raw_text)
    return raw_text


def _build_agent1_prompt(flow_name: str, target_url: str, actions_json: str, fields_context: str, missing_scenarios: list = None, test_types: str = "") -> str:
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
    
    return f"""
    You are Agent 1: The Functional Test Case Generator (The Builder).
    Your ONLY job is to generate POSITIVE, CRUD, WORKFLOW, FIELD VALIDATION, and UI FUNCTIONAL test cases.
    {type_filter_instruction}
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
    
    CRITICAL RULES:
    - DO NOT generate login/authentication steps. Assume already logged in.
    - Each test case MUST start with navigation steps to reach the target module page.
    - Use the EXACT selectors from the recorded actions wherever possible.
    - For dropdowns populated by master tables, use realistic active option names.
    - Generate between 15-25 test cases (or as many as needed to cover all functional scenarios).
    
    Output ONLY a valid JSON array of test case objects in this format:
    [
        {{
            "scenario_name": "Descriptive scenario name",
            "type": "positive" | "positive_ui" | "positive_boundary",
            "steps": [
                {{"action": "CLICK", "selector": "CSS selector"}},
                {{"action": "TYPE", "selector": "CSS selector", "value": "input value"}},
                {{"action": "VERIFY_DISABLED", "selector": "CSS selector"}},
                {{"action": "VERIFY_NON_ZERO", "selector": "CSS selector"}}
            ],
            "expected_result": "What should happen after execution"
        }}
    ]
    """


def _build_agent2_prompt(flow_name: str, target_url: str, actions_json: str, fields_context: str, missing_scenarios: list = None, test_types: str = "") -> str:
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
    
    return f"""
    You are Agent 2: The Edge Case & Security Test Generator (The Breaker).
    Your ONLY job is to generate BOUNDARY, NEGATIVE, SECURITY, INACTIVE EXCLUSION, and ERROR HANDLING test cases.
    {type_filter_instruction}
    
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
    
    CRITICAL RULES:
    - DO NOT generate login/authentication steps. Assume already logged in.
    - Each test case MUST start with navigation steps to reach the target module page.
    - Use the EXACT selectors from the recorded actions wherever possible.
    - Generate between 15-25 test cases (or as many as needed to cover all edge cases).
    - For boundary tests, write out full strings literally (e.g., "AAAAAA..." not "A".repeat(50)).
    
    Output ONLY a valid JSON array of test case objects in this format:
    [
        {{
            "scenario_name": "Descriptive scenario name",
            "type": "negative" | "boundary" | "negative_boundary" | "security" | "positive_boundary",
            "steps": [
                {{"action": "CLICK", "selector": "CSS selector"}},
                {{"action": "TYPE", "selector": "CSS selector", "value": "input value"}},
                {{"action": "VERIFY_DISABLED", "selector": "CSS selector"}},
                {{"action": "VERIFY_NON_ZERO", "selector": "CSS selector"}}
            ],
            "expected_result": "What should happen after execution"
        }}
    ]
    """


def _build_agent3_prompt(merged_cases: list, flow_name: str, fields_context: str) -> str:
    """Build Agent 3 (QA Reviewer) prompt."""
    return f"""
    You are Agent 3: The QA Coverage Reviewer & Auditor.
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
