"""User-authored test cases: a sheet a tester fills in, no AI and no selectors.

The generate tab's other path. The platform tells the tester what the module
contains (``module inventory``), hands them a CSV template whose columns are the
module's real field labels, and turns each filled row into a runnable scenario:

    Scenario | Type | <Field Label> | <Field Label> | ... | Expected Result

The tester writes plain values and a checkable expected result. Everything
technical is derived here:

* **No selectors.** A column is matched to a field by its label, against the
  inventory the engine already has (``learned.json`` first, the recording as
  fallback). A column that matches nothing is *reported, not guessed* — same
  rule as the defect importer, and for the same reason: a scenario quietly built
  on an invented selector would fail without saying anything about the
  application.
* **No steps.** Each row becomes: open the Add form (when the module has one),
  fill the named fields in column order, submit. The verdict engine then judges
  the outcome from the evidence on screen — the UI check is automatic, the
  sheet never describes the UI.
* **No replacement.** Rows are upserted into ``test_cases.json`` by scenario
  name: a re-upload updates its own rows and never touches the generated ones.

Cell conventions, kept deliberately small:

* empty cell         → the field is not touched
* ``(blank)``        → the field is explicitly cleared (a required-field test)
* anything else      → typed, or selected when the field is a dropdown
* checkbox fields    → yes/no · true/false · 1/0
"""

from __future__ import annotations

import csv
import io
import re

from . import config
from . import learned
from .defects import _rows_from_csv, _rows_from_xlsx, DefectImportError

log = config.get_logger("user_cases")

SCENARIO_HEADERS = ("scenario", "scenario name", "test case", "test case name",
                    "name", "title")
TYPE_HEADERS = ("type", "test type", "category", "priority")
EXPECTED_HEADERS = ("expected result", "expected", "expected behaviour",
                    "expected behavior", "result")

BLANK_MARKERS = ("(blank)", "(empty)", "(clear)", "<blank>", "<empty>")
CHECKED_VALUES = ("yes", "true", "1", "check", "checked", "on")
UNCHECKED_VALUES = ("no", "false", "0", "uncheck", "unchecked", "off")

ORIGIN_TAG = "user"


def _norm(text: str) -> str:
    return re.sub(r"[\s_*]+", " ", (text or "").strip().lower()).strip()


def _looks_like_selector(text: str) -> bool:
    stripped = (text or "").strip()
    return stripped.startswith(("#", ".", "[")) or "[" in stripped


# ──────────────────────────────────────────────────────────────────────
# The inventory — "what is there in the application"
# ──────────────────────────────────────────────────────────────────────
def module_inventory(fields: list[dict], actions: list[dict], *,
                     add_selector: str = "", submit_selector: str = "",
                     target_url: str = "") -> dict:
    """Everything a tester needs to write cases for this module, in one payload.

    Buttons come from the recording; fields from the learned profile with the
    recording as fallback (the caller passes the merged list). Observed effect
    messages are included because they are the exact wording an expected result
    can quote.
    """
    buttons: list[dict] = []
    seen: set[str] = set()
    messages: list[str] = []
    for act in actions or []:
        if (act.get("action") or "").upper() in ("CLICK", "DOUBLE_CLICK"):
            selector = (act.get("selector") or "").strip()
            text = (act.get("text") or act.get("accessible_name")
                    or act.get("label") or "").strip()
            if selector and selector not in seen:
                seen.add(selector)
                buttons.append({"text": text, "selector": selector})
        for message in ((act.get("effect") or {}).get("messages") or []):
            wording = (message.get("text") or "").strip()
            if wording and wording not in messages:
                messages.append(wording)

    return {
        "target_url": target_url,
        "fields": [{
            "label": str(f.get("label") or "").rstrip("* ").strip() or f.get("selector"),
            "selector": f.get("selector"),
            "kind": f.get("kind") or "text",
            "required": bool(f.get("required")),
            "options": (f.get("options") or [])[:12],
            "sensitive": learned.is_sensitive(f),
        } for f in fields or []],
        "buttons": buttons[:20],
        "add_selector": add_selector,
        "submit_selector": submit_selector,
        "observed_messages": messages[:12],
    }


# ──────────────────────────────────────────────────────────────────────
# The template the tester downloads
# ──────────────────────────────────────────────────────────────────────
def _sample_value(field: dict) -> str:
    """A plausible value for the happy-path sample row. Never a credential —
    sensitive fields get the run-time placeholder the engine already resolves."""
    if learned.is_sensitive(field):
        label = _norm(str(field.get("label") or ""))
        return "{{username}}" if "user" in label or "mail" in label else "{{password}}"
    kind = str(field.get("kind") or "text").lower()
    options = field.get("options") or []
    if options:
        return str(options[0])
    if kind in ("number", "numeric", "tel", "spinbutton"):
        return "50"
    if kind in ("date", "datetime", "calendar"):
        return "2026-01-01"
    if kind in ("checkbox",):
        return "yes"
    label = str(field.get("label") or "value").rstrip("* ").strip()
    return f"Test {label}"[:40]


def template_csv(fields: list[dict]) -> str:
    """The CSV a tester fills in: real labels as columns, sample rows to copy.

    Sample rows are prefixed ``SAMPLE — `` so nobody mistakes them for their own
    cases; the importer skips them unless the prefix is removed.
    """
    labels = [str(f.get("label") or f.get("selector") or "").rstrip("* ").strip()
              for f in fields or []]
    labels = [l for l in labels if l]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Scenario", "Type"] + labels + ["Expected Result"])

    if fields:
        happy = ["SAMPLE — Save with valid values", "positive"]
        happy += [_sample_value(f) for f in fields]
        happy += ["The record is saved successfully and appears in the list"]
        writer.writerow(happy)

        required = next((f for f in fields if f.get("required")), None)
        target = required or fields[0]
        target_label = str(target.get("label") or target.get("selector")
                           ).rstrip("* ").strip()
        negative = ["SAMPLE — " + (f"Empty {target_label} is rejected"
                                   if required else
                                   f"Blank {target_label} is rejected"),
                    "negative"]
        negative += ["(blank)" if f is target else _sample_value(f) for f in fields]
        negative += [f"A validation message states that {target_label} is required "
                     f"and the record is not saved"]
        writer.writerow(negative)

    return buffer.getvalue()


# ──────────────────────────────────────────────────────────────────────
# Reading a filled sheet
# ──────────────────────────────────────────────────────────────────────
def read_rows(data: bytes, filename: str) -> list[list[str]]:
    lowered = (filename or "").lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        return _rows_from_xlsx(data)
    if lowered.endswith((".csv", ".tsv", ".txt")):
        return _rows_from_csv(data)
    raise DefectImportError(
        f"Unsupported file type '{filename}'. Upload the filled .csv template "
        f"(or .xlsx).")


def _resolve_headers(headers: list[str], fields: list[dict]) -> tuple[dict, dict, list[str]]:
    """Map column index → meaning. Returns (special, field_columns, unresolved).

    ``special`` holds the scenario/type/expected columns; ``field_columns`` maps
    an index to the inventoried field it names. A header that resolves to
    nothing is reported — a value under it would otherwise be silently dropped,
    which changes what the test means.
    """
    by_label: dict[str, dict] = {}
    by_selector: dict[str, dict] = {}
    for field in fields or []:
        label = _norm(str(field.get("label") or ""))
        if label and label not in by_label:
            by_label[label] = field
        selector = str(field.get("selector") or "").strip()
        if selector:
            by_selector[selector] = field

    special: dict[int, str] = {}
    field_columns: dict[int, dict] = {}
    unresolved: list[str] = []
    for index, raw in enumerate(headers):
        name = _norm(raw)
        if not name:
            continue
        if name in SCENARIO_HEADERS and "scenario" not in special.values():
            special[index] = "scenario"
        elif name in TYPE_HEADERS and "type" not in special.values():
            special[index] = "type"
        elif name in EXPECTED_HEADERS and "expected" not in special.values():
            special[index] = "expected"
        elif name in by_label:
            field_columns[index] = by_label[name]
        elif _looks_like_selector(raw) and raw.strip() in by_selector:
            field_columns[index] = by_selector[raw.strip()]
        else:
            unresolved.append(raw.strip() or f"(column {index + 1})")
    return special, field_columns, unresolved


def _field_step(field: dict, value: str) -> dict:
    """One concrete step for one cell — the only place UI detail is created."""
    selector = field.get("selector")
    kind = str(field.get("kind") or "text").lower()
    if value.strip().lower() in BLANK_MARKERS:
        return {"action": "CLEAR", "selector": selector}
    if kind == "checkbox":
        checked = value.strip().lower() in CHECKED_VALUES
        return {"action": "CHECK" if checked else "UNCHECK", "selector": selector}
    if kind in ("select", "dropdown", "combobox") or field.get("options"):
        return {"action": "SELECT", "selector": selector, "value": value}
    return {"action": "TYPE", "selector": selector, "value": value}


def build_cases(rows: list[list[str]], fields: list[dict], *,
                add_selector: str = "", submit_selector: str = "") -> dict:
    """Filled rows → runnable scenarios, plus a precise account of what was not.

    Never guesses: a row with values in an unrecognised column, no name, or no
    expected result is returned under ``blocked`` with the reason, because an
    imported case that silently means something different from what the tester
    wrote is worse than no case at all.
    """
    if not rows:
        raise DefectImportError("The sheet is empty.")

    special, field_columns, unresolved = _resolve_headers(rows[0], fields)
    if "scenario" not in special.values() or "expected" not in special.values():
        raise DefectImportError(
            "The header row must keep the 'Scenario' and 'Expected Result' "
            "columns from the template. First row reads: "
            + (", ".join(cell for cell in rows[0] if cell) or "(blank)"))
    if not field_columns:
        known = ", ".join(sorted({str(f.get('label') or f.get('selector'))
                                  .rstrip('* ').strip() for f in fields or []})) \
                or "none learned yet — record or run the module first"
        raise DefectImportError(
            "No column matches a field on this module. Column headers must be "
            f"the field labels. Known fields: {known}.")

    index_of = {meaning: idx for idx, meaning in special.items()}
    cases: list[dict] = []
    blocked: list[dict] = []

    for row_number, row in enumerate(rows[1:], start=2):
        def cell(idx: int) -> str:
            return row[idx].strip() if idx < len(row) else ""

        name = cell(index_of["scenario"])
        if not name:
            continue                                   # a spacer line, not an error
        if name.upper().startswith("SAMPLE"):
            continue                                   # the template's own examples

        expected = cell(index_of["expected"])
        stray = [(rows[0][idx].strip() if idx < len(rows[0]) and rows[0][idx].strip()
                  else f"(column {idx + 1})")
                 for idx, value in enumerate(row)
                 if value.strip() and idx not in field_columns and idx not in special]
        if stray:
            blocked.append({"row": row_number, "scenario": name,
                            "reason": ("has values under unrecognised column(s) "
                                       f"{', '.join(sorted(set(stray)))} — matching no "
                                       "field on this module, so the case would not "
                                       "test what the sheet says")})
            continue
        if not expected:
            blocked.append({"row": row_number, "scenario": name,
                            "reason": ("no Expected Result — the run would have "
                                       "nothing to verify, and an unverifiable "
                                       "scenario is never a pass")})
            continue

        steps: list[dict] = []
        if add_selector:
            steps.append({"action": "CLICK", "selector": add_selector})
        touched = False
        for idx in sorted(field_columns):
            value = cell(idx)
            if not value:
                continue
            steps.append(_field_step(field_columns[idx], value))
            touched = True
        if not touched:
            blocked.append({"row": row_number, "scenario": name,
                            "reason": "every field cell is empty — nothing to do"})
            continue
        steps.append({"action": "CLICK",
                      "selector": submit_selector or 'button:has-text("Save")'})

        case_type = (cell(index_of["type"]) if "type" in index_of else "") or "functional"
        cases.append({
            "scenario_name": name[:150],
            "type": case_type.strip().lower()[:30],
            "steps": steps,
            "expected_result": expected,
            "_generated": ORIGIN_TAG,
        })

    return {"cases": cases, "blocked": blocked, "unresolved_columns": unresolved}


# ──────────────────────────────────────────────────────────────────────
# Merging into the suite
# ──────────────────────────────────────────────────────────────────────
def merge_cases(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    """Upsert by scenario name. Returns ``(merged, added, updated)``.

    A re-uploaded sheet updates its own rows in place; everything else in the
    suite — generated, coverage, hand-edited — keeps its position and content.
    """
    merged = [dict(case) for case in existing or []]
    position = {str(case.get("scenario_name") or "").strip().lower(): index
                for index, case in enumerate(merged)}
    added = updated = 0
    for case in incoming:
        key = str(case.get("scenario_name") or "").strip().lower()
        if key in position:
            merged[position[key]] = dict(case)
            updated += 1
        else:
            position[key] = len(merged)
            merged.append(dict(case))
            added += 1
    return merged, added, updated
