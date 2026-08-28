"""Turn a filled Accurate-Data Worksheet into runnable test cases.

This automates Step 6 of the Accurate Data Playbook — the one manual step, where
a human handed the worksheet over and got back an upload-ready sheet. The
worksheet collects module *data* (fields, the exact messages seen on screen,
test data, confirmed rules); this module converts that data into *scenarios* in
the platform's own template, then reuses ``user_cases`` to build the runnable
steps and merge them.

Two principles carried straight from the playbook:

* **Grounded, never invented.** Scenarios are only ever built for the field
  structure the platform already knows (selectors come from the module
  inventory, exactly as the "Write your own" path does — a worksheet field with
  no matching inventory field is *reported*, never turned into a guessed
  selector). Expected results quote the messages the tester copied from the
  screen; where a message was not harvested a generic sentence is used and that
  is stated, so the tester knows which rows will judge cleanly and which need a
  Step-7 pass.
* **Bounded scenario set.** Only the three families the worksheet's own MESSAGE
  rows describe are generated: the happy path (success message), one empty case
  per required field (required message), and a duplicate (duplicate message +
  the "record for duplicate test" value). Boundary/injection permutations remain
  the job of the Coverage feature; inventing them here would be the guessing the
  playbook exists to prevent. Anything captured but not converted is surfaced.

Nothing here changes the run-time path: the generated rows go through
``user_cases.build_cases`` and the ordinary verdict engine, identical to a sheet
a tester typed by hand.
"""

from __future__ import annotations

import csv
import io

from . import config
from . import learned
from . import user_cases
from .defects import _rows_from_csv, _rows_from_xlsx, DefectImportError

log = config.get_logger("worksheet")

# The Item cell of the row that *describes* a section's columns — skipped, so it
# is never mistaken for data.
SECTION_DESCRIPTOR = {
    "field": "field label",
    "message": "when it appears",
    "data": "purpose",
    "rules": "question for",
}

# Trigger wording (the worksheet's Item cell in a MESSAGE row) → the role the
# message plays when a scenario quotes it.
MESSAGE_ROLE_MARKERS = {
    "required": ("required", "empty", "mandatory", "blank"),
    "duplicate": ("duplicate", "already exists", "exists"),
    "invalid": ("invalid", "wrong", "incorrect", "not valid"),
    "success": ("success", "saved", "save", "login", "logged in", "created"),
}

TEXT_LIKE = ("text", "textbox", "number", "numeric", "spinbutton", "tel",
             "email", "url", "date", "datetime", "calendar", "textarea", "")


def read_rows(data: bytes, filename: str) -> list[list[str]]:
    lowered = (filename or "").lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        return _rows_from_xlsx(data)
    if lowered.endswith((".csv", ".tsv", ".txt")):
        return _rows_from_csv(data)
    raise DefectImportError(
        f"Unsupported file type '{filename}'. Upload the filled worksheet as "
        f".csv or .xlsx.")


# ──────────────────────────────────────────────────────────────────────
# Parsing the worksheet
# ──────────────────────────────────────────────────────────────────────
def parse_worksheet(rows: list[list[str]]) -> dict:
    """Read the MODULE / FIELD / MESSAGE / DATA / RULES sections into a dict.

    Tolerant of the blank template (empty rows), the per-section descriptor row,
    and either capitalisation. Returns only what the tester actually filled in.
    """
    def cell(row: list[str], index: int) -> str:
        return row[index].strip() if index < len(row) else ""

    module: dict[str, str] = {}
    fields: list[dict] = []
    messages: list[dict] = []
    data: list[dict] = []
    rules: list[dict] = []

    start = 0
    if rows and cell(rows[0], 0).lower() == "section":
        start = 1

    for row in rows[start:]:
        section = cell(row, 0).lower()
        item = cell(row, 1)
        if not section or not item:
            continue
        v1, v2, v3, notes = (cell(row, 2), cell(row, 3), cell(row, 4), cell(row, 5))
        descriptor = SECTION_DESCRIPTOR.get(section, "")
        if descriptor and descriptor in item.lower():
            continue                                   # the column-descriptor row

        if section == "module":
            module[item.lower()] = v1
        elif section == "field":
            fields.append({"label": item, "type": v1, "required": v2,
                           "options": v3, "rule": notes})
        elif section == "message":
            if v1:                                     # only harvested wording
                messages.append({"trigger": item, "text": v1})
        elif section == "data":
            if v1 or v2 or v3:
                data.append({"purpose": item, "value1": v1,
                             "value2": v2, "value3": v3})
        elif section == "rules":
            if v1:
                rules.append({"question": item, "answer": v1})

    return {"module": module, "fields": fields, "messages": messages,
            "data": data, "rules": rules}


def _message_role(trigger: str) -> str:
    low = (trigger or "").lower()
    # Duplicate before success: "duplicate record saved?" must not read as success.
    for role in ("required", "duplicate", "invalid", "success"):
        if any(marker in low for marker in MESSAGE_ROLE_MARKERS[role]):
            return role
    return "other"


def _messages_by_role(messages: list[dict]) -> dict[str, str]:
    """First harvested wording for each role. First wins, so the earlier, more
    specific worksheet row is preferred over a later catch-all."""
    by_role: dict[str, str] = {}
    for message in messages:
        role = _message_role(message.get("trigger", ""))
        text = (message.get("text") or "").strip()
        if text and role not in by_role:
            by_role[role] = text
    return by_role


def _required_expected(required_msg: str, label: str, all_labels: list[str]) -> str:
    """Quote the harvested required-field message only when it fits this field.

    A message that names *another* field ("Customer Name is required") must not
    be pasted onto a different field's empty case — it would read as testing the
    wrong thing. Such a case gets a generic sentence; a field-agnostic message
    ("This field is required") is quoted for every field.
    """
    generic = (f"A validation message states that {label} is required and the "
               f"record is not saved")
    if not required_msg:
        return generic
    low = required_msg.lower()
    if label.lower() in low:
        return required_msg
    if any(other.lower() in low for other in all_labels if other != label):
        return generic
    return required_msg          # names no field ⇒ applies to any


def _duplicate_value(data: list[dict]) -> str:
    for row in data:
        if "duplicate" in (row.get("purpose") or "").lower():
            return (row.get("value1") or "").strip()
    return ""


# ──────────────────────────────────────────────────────────────────────
# Worksheet → the platform's upload template
# ──────────────────────────────────────────────────────────────────────
def _labels(fields: list[dict]) -> list[str]:
    return [str(f.get("label") or f.get("selector") or "").rstrip("* ").strip()
            for f in fields]


def _key_field_index(fields: list[dict], parsed: dict) -> int:
    """The field a duplicate collides on: a worksheet 'unique' rule wins, else
    the first required text-like field, else the first text-like field."""
    ws_by_label = {user_cases._norm(f.get("label", "")): f
                   for f in parsed.get("fields") or []}
    for index, field in enumerate(fields):
        ws = ws_by_label.get(user_cases._norm(str(field.get("label") or "")))
        if ws and "unique" in (ws.get("rule") or "").lower():
            return index
    for index, field in enumerate(fields):
        kind = str(field.get("kind") or "text").lower()
        if field.get("required") and kind in TEXT_LIKE and not learned.is_sensitive(field):
            return index
    for index, field in enumerate(fields):
        if str(field.get("kind") or "text").lower() in TEXT_LIKE:
            return index
    return 0


def generate_rows(parsed: dict, fields: list[dict]) -> tuple[list[list[str]], list[str]]:
    """The upload-template rows for this module, plus provenance notes.

    Emits the platform's own template shape — ``Scenario, Type, <labels>,
    Expected Result`` — so the result flows straight through
    ``user_cases.build_cases``. Notes say which expected results quote real
    wording and what worksheet content was captured but not converted.
    """
    labels = _labels(fields)
    header = ["Scenario", "Type"] + labels + ["Expected Result"]
    rows: list[list[str]] = [header]
    notes: list[str] = []

    by_role = _messages_by_role(parsed.get("messages") or [])
    happy = [user_cases._sample_value(field) for field in fields]

    # 1 — happy path
    success = by_role.get("success")
    rows.append(["Create with valid values", "positive"] + happy
                + [success or "The record is saved successfully and appears in the list"])
    notes.append("Happy path: expected result "
                 + ("quotes the success message you harvested."
                    if success else
                    "is generic — harvest the success message (Step 3) to make it exact."))

    # 2 — one empty case per required, text-like field
    required = by_role.get("required")
    empties = 0
    quoted_any = False
    for index, field in enumerate(fields):
        kind = str(field.get("kind") or "text").lower()
        if not field.get("required") or kind not in TEXT_LIKE:
            continue
        values = list(happy)
        values[index] = "(blank)"
        expected = _required_expected(required, labels[index], labels)
        quoted_any = quoted_any or (expected == required)
        rows.append([f"Empty {labels[index]} is rejected", "negative"]
                    + values + [expected])
        empties += 1
    if empties:
        if required and quoted_any:
            notes.append(f"Empty-field checks: {empties} generated; the harvested "
                         "required-field message is quoted where it fits the field, "
                         "generic otherwise.")
        else:
            notes.append(f"Empty-field checks: {empties} generated with a generic "
                         "expected result — harvest a required-field message (Step 3) "
                         "to make them exact.")

    # 3 — duplicate, only when the tester supplied the existing value
    dup_value = _duplicate_value(parsed.get("data") or [])
    if dup_value and fields:
        key = _key_field_index(fields, parsed)
        values = list(happy)
        values[key] = dup_value
        duplicate = by_role.get("duplicate")
        rows.append([f"Duplicate {labels[key]} is refused", "negative"] + values
                    + [duplicate or f"A duplicate error is shown for {labels[key]} "
                       f"and no second record is created"])
        notes.append("Duplicate check: uses the 'record for duplicate test' value; "
                     + ("expected result quotes your duplicate message."
                        if duplicate else
                        "harvest the duplicate message to make it exact."))
    elif not dup_value:
        notes.append("No duplicate check: fill DATA → 'Record for duplicate test' "
                     "with an existing value to generate one.")

    return rows, notes


def _captured_but_unused(parsed: dict) -> list[str]:
    """Worksheet content deliberately not turned into cases — surfaced, so
    nothing the tester copied is silently dropped."""
    unused: list[str] = []
    by_role = _messages_by_role(parsed.get("messages") or [])
    if by_role.get("invalid"):
        unused.append("an invalid-value message — use the Coverage feature or add "
                      "a manual row for the specific invalid input it applies to")
    for row in parsed.get("data") or []:
        if "blocked" in (row.get("purpose") or "").lower() and (row.get("value1")):
            unused.append("a blocked/inactive record — a login-with-blocked-account "
                          "case is application-specific; add it as a manual row")
            break
    rule_count = len(parsed.get("rules") or [])
    if rule_count:
        unused.append(f"{rule_count} confirmed rule(s) — captured for reference; "
                      "translate min/max rules into boundary cases via Coverage")
    return unused


def _rows_to_csv(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue()


def build_from_worksheet(data: bytes, filename: str, fields: list[dict], *,
                         add_selector: str = "", submit_selector: str = "") -> dict:
    """Parse a worksheet and produce runnable cases, or say why it cannot.

    The single entry point both endpoints share. Returns everything a preview
    needs; the import endpoint additionally merges ``cases`` into the suite.
    """
    if not fields:
        raise DefectImportError(
            "No fields are known for this module yet, so nothing can be "
            "generated. Record the module (open its form and fill the fields) or "
            "run it once, then download the pre-filled worksheet.")

    parsed = parse_worksheet(read_rows(data, filename))
    rows, notes = generate_rows(parsed, fields)
    built = user_cases.build_cases(rows, fields, add_selector=add_selector,
                                   submit_selector=submit_selector)

    # Worksheet fields the module inventory does not recognise — reported, so a
    # mistyped label or an unrecorded field is visible rather than silent.
    known = {user_cases._norm(str(f.get("label") or "")) for f in fields}
    unmatched = [f["label"] for f in parsed.get("fields") or []
                 if f.get("label") and user_cases._norm(f["label"]) not in known]

    return {
        "parsed": parsed,
        "rows": rows,
        "csv": _rows_to_csv(rows),
        "cases": built["cases"],
        "blocked": built["blocked"],
        "unresolved_columns": built["unresolved_columns"],
        "notes": notes,
        "captured_unused": _captured_but_unused(parsed),
        "unmatched_fields": unmatched,
    }


# ──────────────────────────────────────────────────────────────────────
# A worksheet pre-filled with the module's fields (Steps 1–2)
# ──────────────────────────────────────────────────────────────────────
def worksheet_template_csv(module_name: str, project_flow: str,
                           fields: list[dict], observed_messages: list[str]) -> str:
    """The worksheet, with its FIELD rows already filled from the inventory.

    Automates the copy-from-the-panel part: the tester downloads this and only
    has to harvest the real messages, test data and rules — the human parts the
    platform genuinely cannot know.
    """
    labels = _labels(fields)
    rows: list[list[str]] = [["Section", "Item", "Value 1", "Value 2", "Value 3", "Notes"]]

    rows.append(["MODULE", "Module Name", module_name, "", "", ""])
    rows.append(["MODULE", "Project / Flow", project_flow, "", "", ""])
    rows.append(["MODULE", "Template header row (paste from downloaded CSV)",
                 ", ".join(["Scenario", "Type"] + labels + ["Expected Result"]),
                 "", "", "ground truth for column names"])

    rows.append(["FIELD", "Field Label (exact)", "Field Type", "Required (yes/no)",
                 "Dropdown Options / Range", "Validation rule (min-max-chars-unique)"])
    for field, label in zip(fields, labels):
        options = ", ".join(str(o) for o in (field.get("options") or [])[:12])
        rows.append(["FIELD", label, str(field.get("kind") or "text"),
                     "yes" if field.get("required") else "no", options,
                     "confirm with developer/spec"])

    rows.append(["MESSAGE", "When it appears (trigger)",
                 "Exact message text (copy from screen)", "", "",
                 "triggered deliberately in the app"])
    for trigger in ("Submit with required field empty",
                    "Wrong credentials / invalid value",
                    "Successful save / login", "Duplicate record"):
        rows.append(["MESSAGE", trigger, "", "", "", ""])
    for observed in observed_messages[:6]:
        rows.append(["MESSAGE", "Other message seen (observed by the platform)",
                     observed, "", "", "confirm this is the real wording"])

    rows.append(["DATA", "Purpose", "Username / Key value",
                 "Password / Other values", "Email / Extra", "keep these stable"])
    rows.append(["DATA", "Valid working record", "", "", "",
                 "must save/log in successfully today"])
    rows.append(["DATA", "Blocked / inactive record", "", "", "",
                 "only if such a state exists"])
    rows.append(["DATA", "Record for duplicate test", "", "", "",
                 "an existing value — enables the duplicate case"])

    rows.append(["RULES", "Question for developer/BA", "Answer", "", "",
                 "ask; do not assume"])
    for question in ("Are values trimmed (leading/trailing spaces)?",
                     "Is the key field case-sensitive?",
                     "Min and max length of each restricted field?",
                     "What happens after N failed attempts?"):
        rows.append(["RULES", question, "", "", "", ""])

    return _rows_to_csv(rows)
