"""Rules — the tester's own knowledge, enforced on every run.

A rule is something you know about the application that no recording can tell
the platform:

    "After saving a Fabric Category it must stay on /fabric-category."
    "Beam Length must reject 0 and negative values."
    "The Excel export must contain every record, not just the visible page."

Each rule does three jobs, which is what makes this worth having:

1. **Enforcement** — every scenario's evidence is checked against the applicable
   rules, so a violation fails the run even if the scenario itself passed. This
   is independent of whatever the AI happened to generate.
2. **Generation** — rules are injected into the Builder/Breaker prompts, so new
   suites are written with your constraints in mind.
3. **Scenarios** — a rule can *produce* its own test case, so an invariant you
   describe once is exercised directly rather than only observed in passing.

Rules live at ``projects/<project>/rules.json`` (whole workspace) and
``projects/<project>/flows/<flow>/rules.json`` (that module only).
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from . import config

log = config.get_logger("rules")

RULES_FILE = "rules.json"

KINDS = (
    "must_not_navigate",   # after an action the URL must not change
    "must_navigate",       # the URL must reach something
    "must_show",           # a message must appear
    "must_not_show",       # a message must never appear
    "field_rejects",       # a field must reject given values
    "field_accepts",       # a field must accept given values
    "export_complete",     # a download must contain at least N rows
    "no_console_errors",   # the page must not log errors
    "row_persists",        # a record must still be listed afterwards
)

SEVERITIES = ("blocker", "major", "minor")


# ──────────────────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────────────────
def project_path(project_name: str) -> Path:
    return config.project_dir(project_name) / RULES_FILE


def flow_path(project_name: str, flow_name: str) -> Path:
    return config.flow_dir(project_name, flow_name) / RULES_FILE


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as err:
        log.warning("Unreadable rules file %s: %s", path, err)
        return []
    if isinstance(data, dict):
        data = data.get("rules", [])
    return [normalise(r) for r in data if isinstance(r, dict)]


def _write(path: Path, rules: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2), encoding="utf-8")


def load(project_name: str, flow_name: str = "") -> list[dict]:
    """Project-wide rules plus this flow's own, in that order."""
    rules = _read(project_path(project_name))
    if flow_name:
        rules += _read(flow_path(project_name, flow_name))
    return rules


def applicable(project_name: str, flow_name: str) -> list[dict]:
    """Enabled rules that apply to this flow."""
    result = []
    for rule in load(project_name, flow_name):
        if not rule.get("enabled", True):
            continue
        scope_flows = rule.get("flows") or []
        if scope_flows and flow_name not in scope_flows:
            continue
        result.append(rule)
    return result


def save(project_name: str, rule: dict, flow_name: str = "") -> dict:
    clean = validate(rule)
    path = flow_path(project_name, flow_name) if flow_name else project_path(project_name)
    rules = _read(path)
    for index, existing in enumerate(rules):
        if existing.get("id") == clean["id"]:
            rules[index] = clean
            break
    else:
        rules.append(clean)
    _write(path, rules)
    return clean


def save_many(project_name: str, new_rules: list[dict], flow_name: str = "") -> list[dict]:
    return [save(project_name, rule, flow_name) for rule in new_rules]


def delete(project_name: str, rule_id: str, flow_name: str = "") -> bool:
    for path in ([flow_path(project_name, flow_name)] if flow_name else []) + \
                [project_path(project_name)]:
        rules = _read(path)
        remaining = [r for r in rules if r.get("id") != rule_id]
        if len(remaining) != len(rules):
            _write(path, remaining)
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Model
# ──────────────────────────────────────────────────────────────────────
def normalise(raw: dict) -> dict:
    return {
        "id": raw.get("id") or f"r{uuid.uuid4().hex[:8]}",
        "title": (raw.get("title") or "").strip() or "Untitled rule",
        "text": (raw.get("text") or "").strip(),          # the original English
        "kind": (raw.get("kind") or "must_show").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "severity": (raw.get("severity") or "major").strip(),
        "flows": list(raw.get("flows") or []),
        # Scope a rule to scenario types. "A failed login must stay on /auth/login"
        # is true for negative cases and false for the positive one, so without
        # this a correct rule would fail a correct test.
        "types": list(raw.get("types") or []),
        "selector": (raw.get("selector") or "").strip(),
        "value": raw.get("value", ""),
        "values": list(raw.get("values") or []),
        "url_contains": (raw.get("url_contains") or "").strip(),
        "min_rows": raw.get("min_rows"),
        "generate_scenario": bool(raw.get("generate_scenario", False)),
        "source": (raw.get("source") or "manual").strip(),   # manual | ai
    }


def validate(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("A rule must be a JSON object.")
    rule = normalise(raw)
    if rule["kind"] not in KINDS:
        raise ValueError(f"Unknown rule kind '{rule['kind']}'. Valid kinds: {', '.join(KINDS)}")
    if rule["severity"] not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}.")
    if rule["kind"] in ("must_show", "must_not_show") and not str(rule["value"]).strip():
        raise ValueError(f"'{rule['kind']}' needs the message text in 'value'.")
    if rule["kind"] in ("must_navigate", "must_not_navigate") and not rule["url_contains"]:
        raise ValueError(f"'{rule['kind']}' needs 'url_contains'.")
    if rule["kind"] in ("field_rejects", "field_accepts"):
        if not rule["selector"]:
            raise ValueError(f"'{rule['kind']}' needs the field 'selector'.")
        if not rule["values"]:
            raise ValueError(f"'{rule['kind']}' needs at least one entry in 'values'.")
    if rule["kind"] == "export_complete" and not rule["min_rows"]:
        raise ValueError("'export_complete' needs 'min_rows'.")
    return rule


# ──────────────────────────────────────────────────────────────────────
# Enforcement
# ──────────────────────────────────────────────────────────────────────
def _same_field(a: str, b: str) -> bool:
    return (a or "").strip().lower().lstrip("#") == (b or "").strip().lower().lstrip("#")


def values_entered_into(steps: list, selector: str) -> list[str]:
    """Values this scenario actually entered into one field.

    Deliberately precise. Matching the rule's values against the whole scenario
    as text produced false accusations — "0" appears inside ``timeout: 20000``
    and inside a code like ``ORD-1001``, which would fail a scenario that never
    touched the field at all.
    """
    entered: list[str] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("action", "")).upper() not in ("TYPE", "FILL", "SELECT", "CLEAR"):
            continue
        if _same_field(step.get("selector", ""), selector):
            entered.append(str(step.get("value", "")))
    return entered


def evaluate(rules: list[dict], evidence, scenario: dict, steps: list | None = None) -> list[dict]:
    """Check every applicable rule against one scenario's evidence.

    Returns a result per rule: ``{id, title, kind, severity, ok, skipped, detail}``.
    A rule that cannot apply to this scenario is *skipped*, never silently passed.
    """
    results: list[dict] = []
    messages = [m["text"] for m in getattr(evidence, "messages", [])]
    lowered = [m.lower() for m in messages]
    parsed_steps = steps if steps is not None else (scenario.get("steps") or [])

    for rule in rules:
        kind = rule["kind"]
        ok = True
        skipped = False
        detail = ""

        scoped_types = rule.get("types") or []
        if scoped_types and (scenario.get("type") or "") not in scoped_types:
            results.append({
                "id": rule["id"], "title": rule["title"], "kind": kind,
                "severity": rule["severity"], "ok": True, "skipped": True,
                "detail": f"applies only to {', '.join(scoped_types)} scenarios.",
            })
            continue

        if kind == "must_show":
            wanted = str(rule["value"]).lower()
            ok = any(wanted in m for m in lowered)
            detail = (f"'{rule['value']}' appeared." if ok
                      else f"'{rule['value']}' never appeared on screen.")

        elif kind == "must_not_show":
            wanted = str(rule["value"]).lower()
            hit = next((m for m in messages if wanted in m.lower()), "")
            ok = not hit
            detail = ("not shown." if ok
                      else f"forbidden message appeared: {hit!r}")

        elif kind == "must_not_navigate":
            end = (getattr(evidence, "url_end", "") or "")
            start = (getattr(evidence, "url_start", "") or "")
            if not end or not start:
                skipped, detail = True, "no URL recorded for this scenario."
            else:
                ok = rule["url_contains"].lower() in end.lower()
                detail = (f"stayed on '{rule['url_contains']}'." if ok
                          else f"left '{rule['url_contains']}' and ended on {end!r}.")

        elif kind == "must_navigate":
            end = (getattr(evidence, "url_end", "") or "")
            if not end:
                skipped, detail = True, "no URL recorded for this scenario."
            else:
                ok = rule["url_contains"].lower() in end.lower()
                detail = (f"reached '{rule['url_contains']}'." if ok
                          else f"never reached '{rule['url_contains']}' (ended on {end!r}).")

        elif kind in ("field_rejects", "field_accepts"):
            entered = values_entered_into(parsed_steps, rule["selector"])
            used = [v for v in rule["values"]
                    if any(str(v).strip().lower() == e.strip().lower() for e in entered)]
            if not used:
                skipped = True
                detail = f"this scenario does not enter those values into '{rule['selector']}'."
            else:
                has_error = any(m["kind"] == "error" for m in evidence.messages)
                if kind == "field_rejects":
                    ok = has_error
                    detail = ("the value was rejected with a validation message." if ok
                              else f"{rule['selector']} accepted {used!r} with no validation "
                                   f"message — the rule says it must be rejected.")
                else:
                    ok = not has_error
                    detail = ("the value was accepted." if ok
                              else f"{rule['selector']} rejected {used!r}, but the rule says "
                                   f"it must be accepted.")

        elif kind == "export_complete":
            downloads = getattr(evidence, "downloads", [])
            if not downloads:
                skipped, detail = True, "this scenario downloaded nothing."
            else:
                counted = [d for d in downloads if d.get("row_count") is not None]
                if not counted:
                    skipped, detail = True, "row count unavailable for the downloaded file."
                else:
                    best = max(d["row_count"] for d in counted)
                    ok = best >= int(rule["min_rows"])
                    detail = (f"export contained {best} rows." if ok
                              else f"export contained only {best} rows, fewer than the "
                                   f"{rule['min_rows']} required.")

        elif kind == "no_console_errors":
            severe = [e for e in getattr(evidence, "console_errors", [])
                      if e.lower().startswith("[error]")]
            ok = not severe
            detail = ("no console errors." if ok
                      else f"{len(severe)} console error(s): {severe[0][:140]}")

        elif kind == "row_persists":
            wanted = str(rule["value"]).lower()
            found = any(a.get("ok") and "row" in str(a.get("action", "")).lower()
                        for a in getattr(evidence, "assertions", []))
            if not wanted:
                skipped, detail = True, "no record value configured."
            else:
                ok = found or any(wanted in m for m in lowered)
                detail = ("the record was still present." if ok
                          else f"could not confirm '{rule['value']}' is still listed.")

        else:
            skipped, detail = True, f"rule kind '{kind}' is not enforceable yet."

        results.append({
            "id": rule["id"], "title": rule["title"], "kind": kind,
            "severity": rule["severity"], "ok": ok if not skipped else True,
            "skipped": skipped, "detail": detail,
        })

    return results


def violations(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["skipped"] and not r["ok"]]


# ──────────────────────────────────────────────────────────────────────
# Rules → test scenarios
# ──────────────────────────────────────────────────────────────────────
def to_scenarios(rules: list[dict]) -> list[dict]:
    """Turn rules marked ``generate_scenario`` into runnable test cases."""
    scenarios: list[dict] = []
    for rule in rules:
        if not rule.get("enabled", True) or not rule.get("generate_scenario"):
            continue
        kind = rule["kind"]

        if kind == "field_rejects":
            for value in rule["values"]:
                scenarios.append({
                    "scenario_name": f"[rule] {rule['selector']} rejects {value!r}",
                    "type": "negative",
                    "_generated": "rule", "_rule_id": rule["id"],
                    "steps": [
                        {"action": "CLEAR", "selector": rule["selector"]},
                        {"action": "TYPE", "selector": rule["selector"], "value": str(value)},
                        {"action": "CLICK", "selector": "button:has-text('Save')"},
                        {"action": "VERIFY_MESSAGE", "value": ""},
                    ],
                    "expected_result": (
                        f"A validation error appears because {rule['selector']} must reject "
                        f"{value!r}. ({rule['title']})"
                    ),
                })

        elif kind == "field_accepts":
            for value in rule["values"]:
                scenarios.append({
                    "scenario_name": f"[rule] {rule['selector']} accepts {value!r}",
                    "type": "positive",
                    "_generated": "rule", "_rule_id": rule["id"],
                    "steps": [
                        {"action": "CLEAR", "selector": rule["selector"]},
                        {"action": "TYPE", "selector": rule["selector"], "value": str(value)},
                        {"action": "CLICK", "selector": "button:has-text('Save')"},
                    ],
                    "expected_result": (
                        f"The record saves successfully because {rule['selector']} must accept "
                        f"{value!r}. ({rule['title']})"
                    ),
                })

        elif kind == "must_not_navigate":
            scenarios.append({
                "scenario_name": f"[rule] Stays on {rule['url_contains']} after saving",
                "type": "positive",
                "_generated": "rule", "_rule_id": rule["id"],
                "steps": [
                    {"action": "CLICK", "selector": "button:has-text('Save')"},
                    {"action": "VERIFY_URL", "value": rule["url_contains"]},
                ],
                "expected_result": (
                    f"The page stays on {rule['url_contains']} after saving. ({rule['title']})"
                ),
            })

        elif kind == "export_complete":
            scenarios.append({
                "scenario_name": f"[rule] Export contains at least {rule['min_rows']} rows",
                "type": "positive",
                "_generated": "rule", "_rule_id": rule["id"],
                "steps": [
                    {"action": "CLICK", "selector": rule["selector"] or "#exportBtn"},
                    {"action": "WAIT", "value": "1500"},
                    {"action": "VERIFY_DOWNLOAD", "value": f"at least {rule['min_rows']} rows"},
                ],
                "expected_result": (
                    f"The downloaded file contains every record — at least "
                    f"{rule['min_rows']} rows. ({rule['title']})"
                ),
            })

        elif kind == "must_show":
            scenarios.append({
                "scenario_name": f"[rule] Shows {str(rule['value'])[:40]!r}",
                "type": "positive",
                "_generated": "rule", "_rule_id": rule["id"],
                "steps": [{"action": "VERIFY_MESSAGE", "value": str(rule["value"])}],
                "expected_result": f"'{rule['value']}' is displayed. ({rule['title']})",
            })

    return scenarios


# ──────────────────────────────────────────────────────────────────────
# Prompt text for the generation agents
# ──────────────────────────────────────────────────────────────────────
def as_prompt_block(rules: list[dict]) -> str:
    """Render rules for injection into the Builder / Breaker / Auditor prompts."""
    active = [r for r in rules if r.get("enabled", True)]
    if not active:
        return ""
    lines = [
        "TESTER-SUPPLIED RULES — these come from someone who knows this application.",
        "Treat them as hard requirements: generate scenarios that prove each one, and",
        "never generate a scenario that contradicts one.",
    ]
    for rule in active:
        detail = rule.get("text") or rule.get("title")
        extra = []
        if rule.get("types"):
            extra.append(f"types={rule['types']}")
        if rule.get("selector"):
            extra.append(f"field={rule['selector']}")
        if rule.get("values"):
            extra.append(f"values={rule['values']}")
        if rule.get("url_contains"):
            extra.append(f"url~{rule['url_contains']}")
        if rule.get("min_rows"):
            extra.append(f"min_rows={rule['min_rows']}")
        suffix = f"  [{rule['kind']}{'; ' + ', '.join(extra) if extra else ''}]"
        lines.append(f"- {detail}{suffix}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Plain English → structured rules
# ──────────────────────────────────────────────────────────────────────
TRANSLATE_PROMPT = """You convert a tester's plain-English rules into structured JSON rules
for an automated QA platform.

Available kinds and their required fields:
- must_show           value        (a message that must appear)
- must_not_show       value        (a message that must never appear)
- must_navigate       url_contains (the URL must reach this)
- must_not_navigate   url_contains (the URL must stay on this)
- field_rejects       selector, values[]  (the field must reject these values)
- field_accepts       selector, values[]  (the field must accept these values)
- export_complete     min_rows, selector? (a download must have at least N rows)
- no_console_errors   (no fields)
- row_persists        value        (the record must remain listed)

Context — selectors seen in this module's recording:
{fields}

Rules written by the tester (one per line or paragraph):
{text}

Return ONLY a JSON array. One object per rule:
{{"title": "short label", "text": "the tester's original wording",
  "kind": "one of the kinds above", "severity": "blocker|major|minor",
  "selector": "css selector if relevant else ''",
  "value": "string if relevant else ''",
  "values": ["..."], "url_contains": "", "min_rows": null,
  "generate_scenario": true}}

Rules:
- Prefer a selector that actually appears in the context above; if none fits, leave it empty.
- Set generate_scenario to true when the rule can be proven by a short scenario
  (field_rejects, field_accepts, export_complete, must_not_navigate), false otherwise.
- Never invent rules the tester did not write. One input rule -> one output object.
"""


def build_translate_prompt(text: str, fields_context: str = "") -> str:
    return TRANSLATE_PROMPT.format(text=text.strip(),
                                   fields=fields_context.strip() or "(none recorded)")


def coerce_translated(items) -> list[dict]:
    """Validate what the model returned, discarding anything unusable."""
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            item.setdefault("source", "ai")
            out.append(validate(item))
        except ValueError as err:
            log.info("Discarded a translated rule: %s", err)
    return out
