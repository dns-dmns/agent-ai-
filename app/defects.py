"""Import a defect log and re-test the bugs it records.

A separate feature, deliberately kept at arm's length from the main flow. A
defect sheet is a list of things already known to be broken, which is a different
job from a generated suite:

* Its scenarios never enter a flow's ``test_cases.json``, so a normal Run and the
  coverage numbers are exactly what they were.
* They live in their own flow — ``<Flow> [defects]`` — with their own recording
  copy, their own ``report.json``, and their own place in the flow list.
* Nothing in ``executor.py`` or ``verdict.py`` changes. This module produces
  ordinary scenarios and the existing engine runs them.

The column names come from a real sheet::

    Defect ID | Date | Module | Description | Steps to Reproduce
              | Expected Result | Actual Result | Priority | Status

**Expected Result is the valuable column.** It is a human-written, checkable
sentence, which is exactly what the verdict engine reads — so a defect row is
usually judged correctly without anything being invented.

**Steps to Reproduce is the hard one.** It is prose. Field names are resolved
against the labels the engine has already inventoried for that module, which is
free and exact. Anything that cannot be resolved that way is *not guessed*: the
defect is returned as needing input, with the reason. A row that quietly becomes
a scenario full of invented selectors would be worse than no row at all.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from . import config

log = config.get_logger("defects")

DEFECTS_FILE = "defects.json"
DEFECT_FLOW_SUFFIX = " [defects]"

#: Header spellings seen in the wild, normalised to our own keys.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "defect_id": ("defect id", "defectid", "id", "bug id", "issue id", "ticket"),
    "date": ("date", "reported on", "raised on"),
    "module": ("module", "screen", "page", "feature", "area"),
    "description": ("description", "summary", "title", "defect description"),
    "steps": ("steps to reproduce", "steps", "reproduction steps", "repro steps"),
    "expected": ("expected result", "expected", "expected behaviour",
                 "expected behavior"),
    "actual": ("actual result", "actual", "actual behaviour", "actual behavior"),
    "priority": ("priority", "severity"),
    "status": ("status", "state"),
}

OPEN_STATUSES = ("open", "new", "reopened", "in progress", "assigned", "deferred")
CLOSED_STATUSES = ("closed", "fixed", "resolved", "done", "verified")


def _normalise_header(name: str) -> str:
    return re.sub(r"[\s_]+", " ", (name or "").strip().lower())


def _map_headers(headers: list[str]) -> dict[int, str]:
    """Column index → our key, for whatever spelling the sheet uses."""
    mapping: dict[int, str] = {}
    for index, raw in enumerate(headers):
        normalised = _normalise_header(raw)
        for key, aliases in COLUMN_ALIASES.items():
            if normalised in aliases and key not in mapping.values():
                mapping[index] = key
                break
    return mapping


# ──────────────────────────────────────────────────────────────────────
# Reading the sheet
# ──────────────────────────────────────────────────────────────────────
class DefectImportError(ValueError):
    """The sheet could not be read, with a reason a human can act on."""


def _rows_from_csv(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    # Sniff the delimiter: exports vary between comma, semicolon and tab.
    sample = text[:4000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    return [[(cell or "").strip() for cell in row]
            for row in csv.reader(io.StringIO(text), dialect) if any(row)]


def _rows_from_xlsx(data: bytes) -> list[list[str]]:
    try:
        import openpyxl                          # noqa: PLC0415 - optional extra
    except ImportError as err:
        raise DefectImportError(
            "Reading .xlsx needs the openpyxl package, which is not installed. "
            "Either run 'pip install openpyxl', or save the sheet as .csv — CSV "
            "needs nothing extra and imports identically."
        ) from err
    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = book[book.sheetnames[0]]
    rows: list[list[str]] = []
    for raw in sheet.iter_rows(values_only=True):
        cells = ["" if cell is None else str(cell).strip() for cell in raw]
        if any(cells):
            rows.append(cells)
    return rows


def read_sheet(data: bytes, filename: str) -> list[dict]:
    """Parse a defect sheet into normalised records.

    Accepts .csv/.tsv natively and .xlsx when openpyxl is available. The header
    row may appear after some preamble, which real exports often have.
    """
    lowered = (filename or "").lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        rows = _rows_from_xlsx(data)
    elif lowered.endswith((".csv", ".tsv", ".txt")):
        rows = _rows_from_csv(data)
    elif lowered.endswith(".xls"):
        raise DefectImportError(
            "The old .xls format is not supported. Save the sheet as .xlsx or .csv."
        )
    else:
        raise DefectImportError(
            f"Unsupported file type '{filename}'. Upload .csv or .xlsx.")

    if not rows:
        raise DefectImportError("The sheet is empty.")

    header_index, headers = -1, {}
    for index, row in enumerate(rows[:10]):
        candidate = _map_headers(row)
        # A usable header row identifies at least the module and the expectation.
        if "module" in candidate.values() and "expected" in candidate.values():
            header_index, headers = index, candidate
            break
    if header_index < 0:
        found = ", ".join(r for r in rows[0] if r) or "(blank)"
        raise DefectImportError(
            "Could not find the header row. A 'Module' column and an 'Expected "
            f"Result' column are required. The first row reads: {found}"
        )

    records: list[dict] = []
    for row in rows[header_index + 1:]:
        record: dict[str, str] = {}
        for index, key in headers.items():
            if index < len(row):
                record[key] = row[index]
        if not (record.get("module") or record.get("defect_id")):
            continue
        records.append(normalise(record))
    if not records:
        raise DefectImportError("The header row was found but no defect rows follow it.")
    return records


def normalise(raw: dict) -> dict:
    status = (raw.get("status") or "Open").strip()
    return {
        "defect_id": (raw.get("defect_id") or "").strip(),
        "date": (raw.get("date") or "").strip(),
        "module": (raw.get("module") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "steps": (raw.get("steps") or "").strip(),
        "expected": (raw.get("expected") or "").strip(),
        "actual": (raw.get("actual") or "").strip(),
        "priority": (raw.get("priority") or "Medium").strip(),
        "status": status,
        "is_open": status.lower() in OPEN_STATUSES or status.lower() not in CLOSED_STATUSES,
    }


# ──────────────────────────────────────────────────────────────────────
# Module → flow
# ──────────────────────────────────────────────────────────────────────
def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def match_flow(module: str, flow_names: list[str]) -> str:
    """Best flow for a module label like ``Master Data -> Paper Core``.

    Exact match first, then the trailing segment, then a token overlap. Returns
    "" rather than a poor guess: a defect aimed at the wrong module would fail
    for reasons that have nothing to do with the defect.
    """
    if not module:
        return ""
    candidates = [f for f in flow_names if not f.endswith(DEFECT_FLOW_SUFFIX)]
    lowered = {f.lower(): f for f in candidates}

    whole = module.strip().lower()
    if whole in lowered:
        return lowered[whole]

    # "Master Data -> Paper Core" → "paper core"
    tail = re.split(r"->|>|/|\\|»|:", module)[-1].strip().lower()
    if tail in lowered:
        return lowered[tail]

    tail_tokens = set(_tokens(tail))
    if tail_tokens:
        scored = []
        for flow in candidates:
            flow_tokens = set(_tokens(flow))
            if not flow_tokens:
                continue
            overlap = len(tail_tokens & flow_tokens)
            if overlap and overlap >= min(len(tail_tokens), len(flow_tokens)):
                scored.append((overlap, flow))
        if scored:
            scored.sort(key=lambda pair: -pair[0])
            return scored[0][1]
    return ""


def defect_flow_name(flow_name: str) -> str:
    return f"{flow_name}{DEFECT_FLOW_SUFFIX}"


# ──────────────────────────────────────────────────────────────────────
# Prose steps → real steps
# ──────────────────────────────────────────────────────────────────────
SPLIT_STEPS = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*•])\s*")

NAVIGATE_WORDS = ("navigate", "go to", "open the page", "browse to", "launch")
ADD_WORDS = ("create", "add", "new ", "click to create")
SUBMIT_WORDS = ("save", "submit", "attempt to save")
OBSERVE_WORDS = ("observe", "check", "verify", "notice", "look at")

#: Relative or unspecified values a sheet cannot supply on its own.
VAGUE_VALUE = re.compile(
    r"\b(a value|any value|some value|less than|greater than|more than|equal to|"
    r"valid|invalid|appropriate)\b", re.IGNORECASE)

FIELD_IN_TEXT = re.compile(r"""["'“”]([^"'“”]{2,60})["'“”]|\b([A-Z][\w()/ .+-]{2,40}?)\s*"""
                           r"""(?:field|dropdown|input|box|value)\b""")


def split_steps(text: str) -> list[str]:
    """One line per numbered step, however the sheet numbered them."""
    if not text:
        return []
    parts = [p.strip() for p in SPLIT_STEPS.split(text) if p and p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in re.split(r"[\n;]+", text) if p.strip()]
    return parts


def _field_by_label(phrase: str, fields: list[dict]) -> dict | None:
    """Match a phrase to an inventoried field by its label.

    Exact-ish label matching only. This is the part that is free and reliable —
    the engine already knows this module's labels — and it is why the fields do
    not have to be guessed.
    """
    text = (phrase or "").lower()
    best, best_len = None, 0
    for field in fields or []:
        label = str(field.get("label") or "").strip().rstrip("*").strip()
        if len(label) < 3:
            continue
        if label.lower() in text and len(label) > best_len:
            best, best_len = field, len(label)
    return best


def steps_from_prose(prose: str, fields: list[dict], *,
                     add_selector: str = "", submit_selector: str = "",
                     expected: str = "",
                     data: dict | None = None) -> tuple[list[dict], list[str], dict]:
    """Turn reproduction prose into steps, and say what could not be turned.

    Returns ``(steps, unresolved)``. Anything in ``unresolved`` is reported to
    the user rather than approximated.
    """
    steps: list[dict] = []
    unresolved: list[str] = []

    suggestions: dict[str, str] = {}

    for line in split_steps(prose):
        low = line.lower()

        if any(word in low for word in NAVIGATE_WORDS):
            continue                    # the run already opens the module's URL

        # Observation before field matching. The other way round, "Observe the
        # field labelled 'Beam Dish'" matched the field by its label and typed
        # the label text into it as a value.
        if any(word in low for word in OBSERVE_WORDS):
            # A labelling defect quotes the correct wording in the expectation.
            wanted = re.findall(r"""["'“”]([^"'“”]{2,60})["'“”]""", expected)
            if wanted:
                steps.append({"action": "VERIFY_TEXT", "selector": "body",
                              "value": wanted[0]})
            else:
                unresolved.append(
                    f'"{line}" — nothing quotable to assert; the expected result '
                    f"does not name the exact text to look for.")
            continue

        if any(word in low for word in SUBMIT_WORDS):
            steps.append({"action": "CLICK",
                          "selector": submit_selector or 'button:has-text("Save")'})
            continue

        if any(word in low for word in ADD_WORDS) and not _field_by_label(line, fields):
            steps.append({"action": "CLICK",
                          "selector": add_selector or 'button:has-text("Add")'})
            continue

        field = _field_by_label(line, fields)
        if field:
            label = str(field.get("label") or field["selector"]).rstrip("* ").strip()
            quoted = re.findall(r"""["'“”]([^"'“”]{1,60})["'“”]""", line)
            if quoted:
                steps.append({"action": "TYPE", "selector": field["selector"],
                              "value": quoted[0]})
                continue
            # A value the user supplied (typically an accepted suggestion) makes
            # the step real without anything being invented behind their back.
            supplied = (data or {}).get(field["selector"])
            if supplied not in (None, ""):
                steps.append({"action": "TYPE", "selector": field["selector"],
                              "value": str(supplied)})
                continue
            # No literal in the sentence. An empty TYPE would be a guess dressed
            # up as a step, so ask instead — and offer a value to accept.
            suggestion = _suggest_value(field, line)
            if suggestion:
                suggestions[field["selector"]] = suggestion
            reason = ("the sheet describes it only in relative terms"
                      if VAGUE_VALUE.search(line)
                      else "the sheet does not say which value to use")
            unresolved.append(
                f'"{line}" — {label} needs a concrete value; {reason}.'
                + (f' Suggested: {suggestion!r}.' if suggestion else ""))
            continue

        unresolved.append(f'"{line}" — no field on this module matches it.')

    return steps, unresolved, suggestions


def _suggest_value(field: dict, line: str) -> str:
    """A value a human can accept, for a step the sheet left relative.

    Offered, never applied silently: a fabricated number that happens to satisfy
    the prose would make the defect look tested when it was not.
    """
    kind = str(field.get("kind") or "text").lower()
    low = (line or "").lower()
    if kind in ("number", "numeric", "tel"):
        if any(word in low for word in ("less than", "smaller", "lower", "below")):
            return "40"
        if any(word in low for word in ("greater than", "larger", "more than", "above")):
            return "60"
        return "50"
    if kind in ("date", "datetime", "calendar"):
        return "2026-01-01"
    if "invalid" in low or "misspell" in low:
        return "Nowhereland"
    return "Test Value"


# ──────────────────────────────────────────────────────────────────────
# Scenario construction
# ──────────────────────────────────────────────────────────────────────
def build_scenario(defect: dict, fields: list[dict], *,
                   add_selector: str = "", submit_selector: str = "",
                   data: dict | None = None) -> dict:
    """One scenario per defect, or a record of why it is not yet runnable."""
    steps, unresolved, suggestions = steps_from_prose(
        defect["steps"], fields, add_selector=add_selector,
        submit_selector=submit_selector, expected=defect["expected"],
        data=data if data is not None else defect.get("data"))

    title = defect["description"] or defect["expected"] or "defect"
    name = f"[{defect['defect_id'] or 'defect'}] {title}".strip()[:150]

    scenario = {
        "scenario_name": name,
        "type": "regression",
        "steps": steps,
        "expected_result": defect["expected"],
        "_generated": "defect",
        "_defect_id": defect["defect_id"],
        "_defect_status": defect["status"],
        # A defect that is still open is EXPECTED to fail. Recording that here is
        # what lets the report say "still open" instead of "regression".
        "_known_defect": bool(defect["is_open"]),
        "_priority": defect["priority"],
    }
    return {
        "defect": defect,
        "scenario": scenario,
        "unresolved": unresolved,
        "suggestions": suggestions,
        "runnable": bool(steps) and not unresolved and bool(defect["expected"]),
    }


# ──────────────────────────────────────────────────────────────────────
# Storage — its own file, never a flow's suite
# ──────────────────────────────────────────────────────────────────────
def path_for(project_name: str) -> Path:
    return config.project_dir(project_name) / DEFECTS_FILE


def load(project_name: str) -> list[dict]:
    target = path_for(project_name)
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as err:
        log.warning("Unreadable defect log for %s: %s", project_name, err)
        return []
    return data if isinstance(data, list) else data.get("defects", [])


def save(project_name: str, defects: list[dict]) -> None:
    target = path_for(project_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(defects, indent=2), encoding="utf-8")


def merge(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    """Upsert by defect id. Returns ``(merged, added, updated)``."""
    by_id = {d.get("defect_id"): dict(d) for d in existing if d.get("defect_id")}
    added = updated = 0
    for defect in incoming:
        key = defect.get("defect_id")
        if not key:
            continue
        if key in by_id:
            by_id[key].update(defect)
            updated += 1
        else:
            by_id[key] = dict(defect)
            added += 1
    merged = [by_id[k] for k in sorted(by_id)]
    return merged, added, updated


def outcome(scenario_status: str, known_defect: bool) -> str:
    """What a result means for a defect, computed outside the verdict engine.

    An open defect that fails is *not news*; an open defect that passes is the
    fix landing; a closed defect that fails is a regression. Collapsing those
    into PASS/FAIL is what would make a defect report unreadable.
    """
    status = (scenario_status or "").upper()
    if status == "PASS":
        return "fixed" if known_defect else "holding"
    if status in ("FAIL", "CRASH"):
        return "still_open" if known_defect else "regressed"
    return "unverified"
