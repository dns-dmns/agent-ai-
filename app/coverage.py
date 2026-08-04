"""Scenarios, their test cases, and coverage you can count.

The platform used to hold one flat list where every entry was called a
"scenario" but was really a single test case: one set of steps, one concrete
input, one expected result. Writing "whitespace name", "255-character name",
"emoji name" and "SQL injection in name" meant four independent entries
repeating the same six steps, and whether all four existed at all depended on
the model remembering to write them.

This module introduces the level that was missing:

    scenario  — the objective, plus a steps template using {{placeholders}}
    test case — one row of concrete data with its own expected result

A scenario carries a ``data_table``; :func:`expand_scenarios` turns it into one
runnable test case per row. Two things follow that were not possible before:

* **Coverage is enumerated, not judged.** :func:`enumerate_field_cases` derives
  the permutations for a field from its own type and constraints — empty,
  boundary, over-boundary, wrong type, injection, duplicate. No model decides
  what to include, so nothing is quietly missed.
* **Results group.** Nine failures against one field read as one finding with
  nine data rows, instead of nine unrelated lines in the report.

:data:`SCENARIO_CHECKLIST` covers the other half of the question — the
behaviours a CRUD module needs regardless of its fields — and
:func:`checklist_gaps` says which of them a suite has not touched.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from . import config, learned

log = config.get_logger("coverage")

# The value a template refers to when a scenario tests exactly one field. Steps
# written as {{value}} work for any field, so an enumerated table stays readable.
VALUE_KEY = "value"

DEFAULT_MAX_TEXT = 255
DEFAULT_MAX_NUMBER = 2_147_483_647          # int32, the usual server-side limit

# Payloads worth trying once per text field. Kept as data so the set is
# auditable rather than buried in branching code.
INJECTION_CASES: tuple[tuple[str, str, str], ...] = (
    ("SQL injection is stored as text",
     "'; DROP TABLE machines;--",
     "The value is treated as literal text — it is stored or rejected with a "
     "validation message, and no database error is shown."),
    ("HTML and script injection is escaped",
     "<script>alert('xss')</script>",
     "The markup is escaped and rendered as text — no alert fires and no raw "
     "HTML reaches the page."),
    ("template injection is not evaluated",
     "{{7*7}}",
     "The value is stored verbatim — the page never shows 49."),
)


# ──────────────────────────────────────────────────────────────────────
# Field-level enumeration
# ──────────────────────────────────────────────────────────────────────
def _case(name: str, value: Any, expected: str, kind: str = "negative") -> dict:
    return {"case_name": name, "values": {VALUE_KEY: value},
            "expected_result": expected, "type": kind}


def _text_cases(label: str, *, required: bool, max_length: int) -> list[dict]:
    cases: list[dict] = []
    if required:
        cases.append(_case(
            "empty is rejected", "",
            f"{label} is mandatory, so the application refuses the submission — "
            f"either a validation message names {label}, or the submit control "
            f"stays disabled or hidden."))
        cases.append(_case(
            "whitespace only is rejected", "   ",
            f"Spaces are not a value: {label} is still treated as empty and the "
            f"submission is refused."))
    cases.append(_case(
        "single character is accepted", "A",
        f"One character is a legal {label} and the record saves.", "boundary"))
    cases.append(_case(
        f"maximum length ({max_length}) is accepted", "A" * max_length,
        f"A {label} of exactly {max_length} characters saves without truncation.",
        "boundary"))
    cases.append(_case(
        f"one over the maximum ({max_length + 1}) is refused", "A" * (max_length + 1),
        f"The application refuses {max_length + 1} characters — a validation "
        f"message appears, or the field itself stops accepting input at "
        f"{max_length}. It must not silently truncate and report success.",
        "boundary"))
    cases.append(_case(
        "leading and trailing spaces are trimmed", "  Trimmed Value  ",
        "The record is stored as 'Trimmed Value' — the surrounding spaces are "
        "removed rather than saved.", "positive"))
    cases.append(_case(
        "special characters are handled", "Test-Value_01 (A/B) &%#",
        f"Punctuation is either accepted as a legal {label} or rejected with a "
        f"validation message. It must not raise a server error."))
    cases.append(_case(
        "unicode and emoji are handled", "Ωmega Ünit 🚀",
        "Non-ASCII text is stored and displayed unchanged, or rejected with a "
        "validation message — never mangled into question marks."))
    for name, payload, expected in INJECTION_CASES:
        cases.append(_case(name, payload, expected, "security"))
    return cases


def _number_cases(label: str, *, required: bool, maximum: int,
                  allow_negative: bool, allow_decimal: bool) -> list[dict]:
    cases: list[dict] = []
    if required:
        cases.append(_case(
            "empty is rejected", "",
            f"{label} is mandatory, so the submission is refused."))
    cases.append(_case("zero is handled", "0",
                       f"Zero is either a legal {label} or rejected with a "
                       f"validation message that names the minimum.", "boundary"))
    if allow_negative:
        cases.append(_case("negative value is accepted", "-1",
                           f"A negative {label} saves.", "boundary"))
    else:
        cases.append(_case(
            "negative value is rejected", "-1",
            f"{label} cannot be negative: a validation message appears, or the "
            f"field refuses the minus sign. A success message here is a defect."))
    cases.append(_case(
        "decimal value is handled", "12.5",
        (f"A fractional {label} is accepted." if allow_decimal
         else f"{label} takes whole numbers only, so the fraction is rejected or "
              f"the field refuses the decimal point."), "boundary"))
    cases.append(_case(
        "non-numeric text is rejected", "abcd",
        "Letters are refused — either a validation message appears or the field "
        "drops the characters as they are typed."))
    cases.append(_case(
        f"maximum ({maximum}) is accepted", str(maximum),
        f"The largest supported {label} saves.", "boundary"))
    cases.append(_case(
        "above the maximum is rejected", str(maximum + 1),
        f"The application refuses a {label} above {maximum} with a readable "
        f"validation message.", "boundary"))
    cases.append(_case(
        "numeric overflow is refused gracefully", "1" + "0" * 18,
        "A value far beyond the column's range is refused with a message written "
        "for a user. A raw server error such as 'Numeric value out of range of "
        "int' reaching the screen is a defect.", "security"))
    return cases


def _email_cases(label: str, *, required: bool) -> list[dict]:
    cases: list[dict] = []
    if required:
        cases.append(_case("empty is rejected", "",
                           f"{label} is mandatory and the submission is refused."))
    cases.append(_case("a valid address is accepted", "qa.tester@example.com",
                       "The address saves.", "positive"))
    cases.append(_case("missing @ is rejected", "qa.tester.example.com",
                       "A format validation message appears."))
    cases.append(_case("missing domain is rejected", "qa.tester@",
                       "A format validation message appears."))
    cases.append(_case("a space inside the address is rejected", "qa tester@example.com",
                       "A format validation message appears."))
    return cases


def _date_cases(label: str, *, required: bool) -> list[dict]:
    cases: list[dict] = []
    if required:
        cases.append(_case("empty is rejected", "",
                           f"{label} is mandatory and the submission is refused."))
    cases.append(_case("an unparseable date is rejected", "31/31/2026",
                       f"{label} refuses an impossible date."))
    cases.append(_case("a far future date is handled", "01/01/2999",
                       f"{label} is either accepted or rejected with a message "
                       f"naming the allowed range.", "boundary"))
    return cases


def _choice_cases(label: str, options: Iterable[str], *, required: bool) -> list[dict]:
    cases: list[dict] = []
    usable = [o for o in options if str(o).strip()][:8]
    if required:
        cases.append(_case("no selection is rejected", "",
                           f"{label} must be chosen, so the submission is refused."))
    for option in usable:
        cases.append(_case(f"'{option}' can be selected", option,
                           f"Choosing '{option}' saves and the record shows it.",
                           "positive"))
    return cases


def enumerate_field_cases(field: dict) -> list[dict]:
    """Every test case one field deserves, derived from the field itself.

    ``field`` is an entry from the learned profile / form inventory:
    ``{selector, label, kind, required, options, max_length}``.

    Returns [] for a field it is not safe to permute — a password, OTP or
    captcha gets filled with a working value, never fuzzed, because a locked
    account fails every later scenario for reasons that have nothing to do
    with them.
    """
    selector = (field.get("selector") or "").strip()
    if not selector:
        return []
    label = (field.get("label") or selector).strip().rstrip("*").strip() or selector
    if learned.is_sensitive(field):
        return []

    kind = (field.get("kind") or "text").strip().lower()
    required = bool(field.get("required"))
    options = field.get("options") or []

    if options or kind in ("select", "dropdown", "radio"):
        return _choice_cases(label, options, required=required)
    if kind in ("number", "numeric", "tel"):
        return _number_cases(
            label, required=required,
            maximum=int(field.get("max_value") or DEFAULT_MAX_NUMBER),
            allow_negative=bool(field.get("allow_negative")),
            allow_decimal=bool(field.get("allow_decimal", True)),
        )
    if kind == "email" or "email" in label.lower():
        return _email_cases(label, required=required)
    if kind in ("date", "datetime", "datetime-local", "calendar"):
        return _date_cases(label, required=required)
    if kind in ("checkbox", "toggle", "switch"):
        return [
            _case("the box can be ticked", "true", f"{label} is saved as on.", "positive"),
            _case("the box can be cleared", "false", f"{label} is saved as off.", "positive"),
        ]
    return _text_cases(label, required=required,
                       max_length=int(field.get("max_length") or DEFAULT_MAX_TEXT))


# ──────────────────────────────────────────────────────────────────────
# Expansion: scenario + data table → runnable test cases
# ──────────────────────────────────────────────────────────────────────
def has_data_table(scenario: dict) -> bool:
    table = scenario.get("data_table")
    return isinstance(table, list) and bool(table)


def _case_type(scenario: dict, row: dict) -> str:
    return (row.get("type") or scenario.get("type") or "functional").strip()


def expand_scenarios(cases: list[dict]) -> list[dict]:
    """Turn every scenario with a ``data_table`` into one case per row.

    Scenarios without a table pass through untouched, so a flat suite recorded
    before this existed keeps running exactly as it did.

    Each produced case keeps ``_parent_name`` and ``_case_name`` so the report
    can group the rows back under their scenario, and ``_data_row`` so the
    existing ``{{placeholder}}`` resolution fills the steps in.
    """
    expanded: list[dict] = []
    for scenario in cases:
        if not has_data_table(scenario):
            expanded.append(scenario)
            continue

        parent = (scenario.get("scenario_name") or "Unnamed scenario").strip()
        rows = [r for r in scenario["data_table"] if isinstance(r, dict)]
        for row_no, row in enumerate(rows, start=1):
            clone = {k: v for k, v in scenario.items() if k != "data_table"}
            values = row.get("values")
            if not isinstance(values, dict):
                # A bare row — {"value": "x", ...} — is still usable.
                values = {k: v for k, v in row.items()
                          if k not in ("case_name", "expected_result", "type")}
            case_name = (row.get("case_name") or f"row {row_no}").strip()

            clone["scenario_name"] = f"{parent} — {case_name}"
            clone["type"] = _case_type(scenario, row)
            clone["_data_row"] = {str(k): v for k, v in values.items()}
            clone["_parent_name"] = parent
            clone["_case_name"] = case_name
            clone["_case_no"] = row_no
            clone["_case_total"] = len(rows)
            if row.get("expected_result"):
                clone["expected_result"] = row["expected_result"]
            expanded.append(clone)
    return expanded


def group_results(checkpoints: list[dict]) -> list[dict]:
    """Collapse expanded cases back under their scenario for reporting.

    Nine data rows against one field become one entry — "7 of 9 passed" — with
    the failures named. A flat suite yields one group per case, which is
    exactly the old presentation.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for point in checkpoints:
        name = point.get("name") or ""
        parent = point.get("parent") or name
        if parent not in groups:
            groups[parent] = {"scenario": parent, "total": 0, "passed": 0,
                              "failed": 0, "unverified": 0, "crashed": 0,
                              "failures": []}
            order.append(parent)
        group = groups[parent]
        group["total"] += 1
        status = (point.get("status") or "").upper()
        if status == "PASS":
            group["passed"] += 1
        elif status == "CRASH":
            group["crashed"] += 1
            group["failures"].append(point.get("case") or name)
        elif status == "UNVERIFIED":
            group["unverified"] += 1
        else:
            group["failed"] += 1
            group["failures"].append(point.get("case") or name)
    for group in groups.values():
        group["verdict"] = ("PASS" if group["passed"] == group["total"]
                            else "CRASH" if group["crashed"]
                            else "FAIL" if group["failed"]
                            else "UNVERIFIED")
        group["summary"] = f"{group['passed']} of {group['total']} test cases passed"
    return [groups[name] for name in order]


# ──────────────────────────────────────────────────────────────────────
# Scenario-level coverage: what a CRUD module needs whatever its fields are
# ──────────────────────────────────────────────────────────────────────
SCENARIO_CHECKLIST: tuple[dict, ...] = (
    {"id": "create_all", "title": "Create with every field filled",
     "match": ("create", "add", "new", "save"), "type": "positive"},
    {"id": "create_min", "title": "Create with only the mandatory fields",
     "match": ("minimum", "mandatory only", "required only"), "type": "positive"},
    {"id": "create_empty", "title": "Empty form is refused",
     "match": ("empty form", "without filling", "blank form", "mandatory field validation"),
     "type": "negative"},
    {"id": "duplicate", "title": "Duplicate record is refused",
     "match": ("duplicate", "already exists", "same name"), "type": "negative"},
    {"id": "read", "title": "Open a record and read its details",
     "match": ("view", "details", "open record"), "type": "positive"},
    {"id": "update", "title": "Edit an existing record",
     "match": ("edit", "update", "modify"), "type": "positive"},
    {"id": "delete", "title": "Delete a record",
     "match": ("delete", "remove"), "type": "positive"},
    {"id": "delete_cancel", "title": "Cancelling a delete keeps the record",
     "match": ("cancel", "no", "abort"), "type": "positive"},
    {"id": "search_exact", "title": "Search finds a record by exact value",
     "match": ("search", "filter", "find"), "type": "positive"},
    {"id": "search_none", "title": "Search with no match reports an empty grid",
     "match": ("no result", "zero result", "no record", "not found", "empty"),
     "type": "negative"},
    {"id": "status", "title": "Activate / deactivate the record",
     "match": ("status", "active", "inactive", "enable", "disable"), "type": "positive"},
    {"id": "export", "title": "Export the list",
     "match": ("export", "download", "excel", "csv"), "type": "functional"},
    {"id": "audit", "title": "Audit / history records who changed what and when",
     "match": ("audit", "history", "audit trail", "change log", "activity log"),
     "type": "functional"},
    {"id": "readonly", "title": "System-generated fields cannot be edited",
     "match": ("read-only", "readonly", "system generated", "non-editable"),
     "type": "negative"},
    {"id": "pagination", "title": "Paging and sorting the grid",
     "match": ("pagination", "page", "sort", "order by"), "type": "ui"},
    {"id": "cancel_dialog", "title": "Closing the dialog discards the entry",
     "match": ("close", "discard", "without saving", "cancel dialog"), "type": "ui"},
)


def _haystack(scenario: dict) -> str:
    parts = [str(scenario.get("scenario_name") or ""),
             str(scenario.get("_parent_name") or ""),
             str(scenario.get("expected_result") or "")]
    for step in scenario.get("steps") or []:
        if isinstance(step, dict):
            parts.append(f"{step.get('action', '')} {step.get('selector', '')} "
                         f"{step.get('value', '')}")
        else:
            parts.append(str(step))
    return " ".join(parts).lower()


def _mentions(blob: str, phrase: str) -> bool:
    """Whole-word match, so a checklist word cannot hide inside another word.

    A plain substring test made "log" match *login* and *dialog*, which reported
    audit-trail coverage that did not exist — the same mistake that once let a
    failed click on ``#taxCode`` fall through to a Cancel button because "x"
    looked like "close".
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", blob) is not None


def checklist_gaps(cases: list[dict]) -> list[dict]:
    """Which checklist behaviours this suite never exercises.

    Deliberately literal: it matches on whole words, so it under-claims rather
    than over-claims. A gap it reports is worth a look; silence is not a proof.
    """
    blobs = [_haystack(c) for c in cases]
    missing = []
    for item in SCENARIO_CHECKLIST:
        if not any(_mentions(blob, word) for blob in blobs for word in item["match"]):
            missing.append({"id": item["id"], "title": item["title"],
                            "type": item["type"]})
    return missing


def coverage_summary(cases: list[dict]) -> dict:
    """Counts a human can check: scenarios, test cases, and what is missing."""
    scenarios = [c for c in cases if has_data_table(c)]
    flat = [c for c in cases if not has_data_table(c)]
    table_cases = sum(len(c["data_table"]) for c in scenarios)
    gaps = checklist_gaps(cases)
    covered = len(SCENARIO_CHECKLIST) - len(gaps)
    return {
        "scenarios": len(scenarios) + len(flat),
        "parameterised_scenarios": len(scenarios),
        "test_cases": table_cases + len(flat),
        "checklist_total": len(SCENARIO_CHECKLIST),
        "checklist_covered": covered,
        "checklist_percent": round(covered * 100 / len(SCENARIO_CHECKLIST)),
        "gaps": gaps,
    }


# ──────────────────────────────────────────────────────────────────────
# Building a field-coverage scenario from a discovered form
# ──────────────────────────────────────────────────────────────────────
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "field"


def field_scenario(field: dict, *, open_steps: list[dict],
                   submit_selector: str, verify: list[dict] | None = None) -> dict | None:
    """One parameterised scenario covering a single field completely.

    ``open_steps`` are the steps that reach the form (click Add, etc.), and are
    replayed for every row — each test case must stand on its own, because a
    row that depends on the previous one fails for the wrong reason.
    """
    rows = enumerate_field_cases(field)
    if not rows:
        return None
    selector = field["selector"]
    label = (field.get("label") or selector).strip().rstrip("*").strip()
    steps = [dict(s) for s in open_steps]
    steps.append({"action": "TYPE", "selector": selector, "value": f"{{{{{VALUE_KEY}}}}}"})
    steps.append({"action": "CLICK", "selector": submit_selector})
    steps.extend(dict(v) for v in (verify or []))
    return {
        "scenario_name": f"{label} accepts and rejects the right values",
        "type": "negative",
        "field_under_test": selector,
        "steps": steps,
        "expected_result": f"Each value is handled according to {label}'s rules.",
        "data_table": rows,
        "_generated": "coverage",
    }


def build_field_scenarios(fields: list[dict], *, open_steps: list[dict],
                          submit_selector: str,
                          verify: list[dict] | None = None) -> list[dict]:
    out = []
    for field in fields or []:
        scenario = field_scenario(field, open_steps=open_steps,
                                  submit_selector=submit_selector, verify=verify)
        if scenario:
            out.append(scenario)
    return out
