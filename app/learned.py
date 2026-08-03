"""What the platform teaches itself about an application.

A recording is a thin trace: it shows the fields the tester happened to touch and
nothing else. Everything the platform later gets wrong about an application —
a required field it never fills, a validation message it cannot see, a submit
button it does not know is gated — comes from treating that trace as the whole
truth.

So the platform observes the real page on every run and keeps what it learns in
``projects/<project>/learned.json``::

    { "flows": {
        "Beamer Machine": {
          "fields": [
            { "selector": "input[placeholder=\\"Enter width\\"]",
              "label": "M/C Width (inch) *", "kind": "number",
              "required": true, "seen": 4 }
          ],
          "submit_gating": "disabled",       # submit stays disabled until valid
          "healed": { "old > structural > selector": "#stableId" }
        } },
      "message_selectors": [".oxd-input-field-error-message"] }

Nothing here is authored by a human, and nothing needs an API key: it is written
by the engine while it drives the browser, and read back by the generator and the
runner on later runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config

log = config.get_logger("learned")

PROFILE_FILE = "learned.json"

# Never invent a value for these, whatever the application says.
NEVER_AUTOFILL = ("password", "otp", "captcha", "token", "secret", "pin")


def path_for(project_name: str) -> Path:
    return config.project_dir(project_name) / PROFILE_FILE


def load(project_name: str) -> dict:
    target = path_for(project_name)
    if not target.exists():
        return {"flows": {}, "message_selectors": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as err:
        log.warning("Unreadable profile for %s: %s", project_name, err)
        return {"flows": {}, "message_selectors": []}
    data.setdefault("flows", {})
    data.setdefault("message_selectors", [])
    return data


def save(project_name: str, profile: dict) -> None:
    target = path_for(project_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    except Exception as err:
        log.warning("Could not persist the profile for %s: %s", project_name, err)


def flow_profile(project_name: str, flow_name: str) -> dict:
    profile = load(project_name)
    flow = profile["flows"].setdefault(flow_name, {})
    flow.setdefault("fields", [])
    flow.setdefault("submit_gating", "unknown")   # unknown | disabled | open
    flow.setdefault("healed", {})
    return flow


# ──────────────────────────────────────────────────────────────────────
# Recording what the browser showed us
# ──────────────────────────────────────────────────────────────────────
def remember_fields(project_name: str, flow_name: str, fields: list[dict]) -> dict:
    """Merge a page inventory into the profile, keeping a 'seen' count.

    Called every run, so a field only present in some dialogs still accumulates
    and a field that disappears is not forgotten immediately.
    """
    if not fields:
        return flow_profile(project_name, flow_name)
    profile = load(project_name)
    flow = profile["flows"].setdefault(flow_name, {})
    flow.setdefault("fields", [])
    flow.setdefault("submit_gating", "unknown")
    flow.setdefault("healed", {})

    by_selector = {f.get("selector"): f for f in flow["fields"] if f.get("selector")}
    for field in fields:
        selector = field.get("selector")
        if not selector:
            continue
        existing = by_selector.get(selector)
        if existing:
            existing["seen"] = int(existing.get("seen", 1)) + 1
            # Required-ness and labels can only get more accurate over time.
            existing["required"] = bool(existing.get("required")) or bool(field.get("required"))
            for key in ("label", "kind", "min", "max", "maxlength", "options"):
                if field.get(key) not in (None, "", []):
                    existing[key] = field[key]
        else:
            entry = dict(field)
            entry["seen"] = 1
            flow["fields"].append(entry)
            by_selector[selector] = entry

    save(project_name, profile)
    return flow


def remember_submit_gating(project_name: str, flow_name: str, gating: str) -> None:
    """Record that this application gates submission behind form validity.

    Knowing this turns "click Save and expect a validation message" — a scenario
    that can never pass on such an application — into "assert Save is disabled".
    """
    if gating not in ("disabled", "open"):
        return
    profile = load(project_name)
    flow = profile["flows"].setdefault(flow_name, {})
    if flow.get("submit_gating") == gating:
        return
    flow["submit_gating"] = gating
    save(project_name, profile)
    log.info("Learned: %s/%s gates submission = %s", project_name, flow_name, gating)


def remember_message_selector(project_name: str, selector: str) -> None:
    """Learn where this application renders messages, so the next run sees them."""
    selector = (selector or "").strip()
    if not selector or len(selector) > 120:
        return
    profile = load(project_name)
    known = profile.setdefault("message_selectors", [])
    # Keep the class-based part; a full path is too brittle to reuse.
    for token in selector.split():
        if token.startswith(".") and token not in known:
            known.append(token)
            save(project_name, profile)
            log.info("Learned message selector for %s: %s", project_name, token)
            return


def remember_healed(project_name: str, flow_name: str, original: str, working: str) -> None:
    """A selector that failed and a replacement that worked — remember the swap."""
    if not original or not working or original == working:
        return
    profile = load(project_name)
    flow = profile["flows"].setdefault(flow_name, {})
    healed = flow.setdefault("healed", {})
    if healed.get(original) == working:
        return
    healed[original] = working
    save(project_name, profile)
    log.info("Learned selector repair in %s/%s: %r -> %r",
             project_name, flow_name, original, working)


# ──────────────────────────────────────────────────────────────────────
# Reading it back
# ──────────────────────────────────────────────────────────────────────
def healed_selector(project_name: str, flow_name: str, selector: str) -> str:
    return flow_profile(project_name, flow_name).get("healed", {}).get(selector, "")


def message_selectors(project_name: str) -> list[str]:
    return list(load(project_name).get("message_selectors") or [])


def required_fields(project_name: str, flow_name: str) -> list[dict]:
    return [f for f in flow_profile(project_name, flow_name)["fields"] if f.get("required")]


def gates_submission(project_name: str, flow_name: str) -> bool:
    return flow_profile(project_name, flow_name).get("submit_gating") == "disabled"


def is_sensitive(field: dict) -> bool:
    blob = " ".join(str(field.get(k, "")) for k in ("selector", "label", "kind", "name")).lower()
    return any(word in blob for word in NEVER_AUTOFILL)


def synthetic_value(field: dict, token: str = "") -> Any:
    """A valid value for a field the platform must fill itself.

    Deliberately conservative: it respects the constraints the DOM advertises so
    the value is accepted, and it is never used for a field the scenario itself
    is testing.
    """
    kind = (field.get("kind") or "text").lower()

    if kind in ("number", "range"):
        low = field.get("min")
        high = field.get("max")
        try:
            value = 1 if low in (None, "") else float(low)
            if value <= 0:
                value = 1
        except (TypeError, ValueError):
            value = 1
        try:
            if high not in (None, "") and value > float(high):
                value = float(high)
        except (TypeError, ValueError):
            pass
        return str(int(value)) if float(value).is_integer() else str(value)

    if kind == "email":
        return f"qa{token or '1'}@example.com"
    if kind == "tel":
        return "9000000000"
    if kind == "url":
        return "https://example.com"
    if kind == "date":
        import datetime
        return datetime.date.today().isoformat()
    if kind == "time":
        return "09:00"
    if kind in ("select", "dropdown"):
        options = [o for o in (field.get("options") or []) if str(o).strip()]
        return options[0] if options else ""

    base = f"QA{token}" if token else "QA Auto"
    try:
        limit = int(field.get("maxlength") or 0)
        if limit and len(base) > limit:
            base = base[:limit]
    except (TypeError, ValueError):
        pass
    return base
