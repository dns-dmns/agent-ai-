"""Dependency contracts — what a flow *provides* and what it *requires*.

The platform could already replay a prerequisite flow before a scenario, but the
prerequisite's result was invisible afterwards: nothing it created could be
referenced, so a dependent module had to hardcode a guess. That is the gap this
module closes.

A contract lives at ``projects/<project>/flows/<flow>/contract.json``::

    {
      "provides": [
        {"entity": "department",
         "var": "department_name",          # becomes {{department_name}}
         "name_field": "#deptName",         # where the value is typed/read
         "status_field": "#status",         # optional: active/inactive control
         "list_url": "/masters/department"} # optional: page listing the records
      ],
      "requires": [
        {"entity": "department",
         "selector": "#departmentDropdown",
         "kind": "dropdown"}                # dropdown | input | lookup
      ],
      "run_once": true                      # seed once per run, not per scenario
    }

With that, the engine can capture the prerequisite's value into the variable
pool, substitute it into the dependent scenario, and generate the relationship
checks that a chain of modules actually needs — most importantly that an
*inactive* master disappears from the dependent dropdown.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from . import storage

log = config.get_logger("contracts")

CONTRACT_FILE = "contract.json"
KINDS = ("dropdown", "input", "lookup")
CAPTURE_MODES = ("typed", "value", "text")

EMPTY: dict = {"provides": [], "requires": [], "run_once": True}


# ──────────────────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────────────────
def path_for(project_name: str, flow_name: str) -> Path:
    return config.flow_dir(project_name, flow_name) / CONTRACT_FILE


def load(project_name: str, flow_name: str) -> dict:
    """Return the contract for a flow, or an empty one."""
    target = path_for(project_name, flow_name)
    if not target.exists():
        return dict(EMPTY, provides=[], requires=[])
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as err:
        log.warning("Unreadable contract for %s/%s: %s", project_name, flow_name, err)
        return dict(EMPTY, provides=[], requires=[])
    return normalise(raw)


def save(project_name: str, flow_name: str, data: dict) -> dict:
    clean = validate(data)
    target = path_for(project_name, flow_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    storage.write_json(target, clean, indent=2)
    return clean


def normalise(raw: dict) -> dict:
    """Fill in defaults without rejecting a hand-written file."""
    provides = []
    for item in raw.get("provides") or []:
        if not isinstance(item, dict):
            continue
        entity = (item.get("entity") or "").strip()
        if not entity:
            continue
        provides.append({
            "entity": entity,
            "var": (item.get("var") or f"{entity}_value").strip(),
            "name_field": (item.get("name_field") or "").strip(),
            "status_field": (item.get("status_field") or "").strip(),
            "active_value": (item.get("active_value") or "Active").strip(),
            "inactive_value": (item.get("inactive_value") or "Inactive").strip(),
            "list_url": (item.get("list_url") or "").strip(),
            "capture": (item.get("capture") or "typed").strip().lower(),
        })

    requires = []
    for item in raw.get("requires") or []:
        if not isinstance(item, dict):
            continue
        entity = (item.get("entity") or "").strip()
        selector = (item.get("selector") or "").strip()
        if not entity or not selector:
            continue
        requires.append({
            "entity": entity,
            "selector": selector,
            "kind": (item.get("kind") or "dropdown").strip().lower(),
            "optional": bool(item.get("optional", False)),
        })

    # What the application considers a duplicate. If the uniqueness key is
    # name + date, two records sharing a name but not a date are legitimately
    # NOT duplicates — so a test that only repeats the name proves nothing.
    unique_by = [str(sel).strip() for sel in (raw.get("unique_by") or [])
                 if str(sel).strip()]

    return {
        "provides": provides,
        "requires": requires,
        "unique_by": unique_by,
        "run_once": bool(raw.get("run_once", True)),
    }


def validate(data: dict) -> dict:
    """Normalise and reject the mistakes that would silently produce dead tests."""
    if not isinstance(data, dict):
        raise ValueError("A contract must be a JSON object.")
    clean = normalise(data)

    seen_vars: set[str] = set()
    for item in clean["provides"]:
        if not item["name_field"]:
            raise ValueError(
                f"provides['{item['entity']}'] needs a 'name_field' — the selector whose "
                f"value should be captured into {{{{{item['var']}}}}}."
            )
        if item["var"] in seen_vars:
            raise ValueError(f"Two provides entries both capture into '{item['var']}'.")
        seen_vars.add(item["var"])
        if item["capture"] not in CAPTURE_MODES:
            raise ValueError(f"capture must be one of {CAPTURE_MODES}, got '{item['capture']}'.")

    for item in clean["requires"]:
        if item["kind"] not in KINDS:
            raise ValueError(f"requires kind must be one of {KINDS}, got '{item['kind']}'.")

    return clean


# ──────────────────────────────────────────────────────────────────────
# Chain resolution
# ──────────────────────────────────────────────────────────────────────
def dependency_list(dependencies: str) -> list[str]:
    return [d.strip() for d in (dependencies or "").split(",") if d.strip()]


def provider_map(project_name: str, dependencies: str) -> dict[str, dict]:
    """entity -> {flow, provide} for every entity the prerequisites supply."""
    providers: dict[str, dict] = {}
    for flow in dependency_list(dependencies):
        contract = load(project_name, flow)
        for provide in contract["provides"]:
            providers[provide["entity"]] = {"flow": flow, "provide": provide}
    return providers


def capture_steps(contract: dict) -> list[dict]:
    """STORE steps appended to a prerequisite replay so its values survive it."""
    steps = []
    for provide in contract.get("provides", []):
        if not provide.get("name_field"):
            continue
        steps.append({
            "action": "STORE",
            "selector": provide["name_field"],
            "value": provide["var"],
            "capture": provide.get("capture", "typed"),
            "_note": f"capture {provide['entity']} into {{{{{provide['var']}}}}}",
        })
    return steps


def unresolved_requirements(project_name: str, flow_name: str,
                            dependencies: str) -> list[dict]:
    """Requirements of a flow that no chained prerequisite provides."""
    contract = load(project_name, flow_name)
    providers = provider_map(project_name, dependencies)
    missing = []
    for need in contract["requires"]:
        if need["optional"]:
            continue
        if need["entity"] not in providers:
            missing.append(need)
    return missing


# ──────────────────────────────────────────────────────────────────────
# Relationship checks
# ──────────────────────────────────────────────────────────────────────
def relationship_checks(project_name: str, flow_name: str,
                        dependencies: str) -> tuple[list[dict], list[str]]:
    """Generate the cross-module scenarios a dependent chain actually needs.

    Returns ``(scenarios, notes)``. Notes explain anything that could not be
    generated, so nothing is skipped silently.
    """
    contract = load(project_name, flow_name)
    providers = provider_map(project_name, dependencies)
    scenarios: list[dict] = []
    notes: list[str] = []

    if not contract["requires"]:
        notes.append(
            f"'{flow_name}' has no 'requires' entries in its contract, so there is no "
            f"relationship to check. Add one naming the dropdown that consumes a master."
        )
        return scenarios, notes

    for need in contract["requires"]:
        entity = need["entity"]
        selector = need["selector"]
        source = providers.get(entity)
        if not source:
            notes.append(
                f"No chained prerequisite provides '{entity}'. Add the master flow to "
                f"Chain prerequisites and give it a contract that provides '{entity}'."
            )
            continue

        provide = source["provide"]
        var = "{{" + provide["var"] + "}}"
        master_flow = source["flow"]

        # 1 ── the active master must be offered downstream
        scenarios.append({
            "scenario_name": f"[dependency] Active {entity} appears in {flow_name}",
            "type": "positive",
            "_generated": "relationship",
            "steps": [
                {"action": "VERIFY_OPTION_PRESENT", "selector": selector, "value": var},
            ],
            "expected_result": (
                f"The {entity} created by '{master_flow}' is selectable in {flow_name}."
            ),
        })

        # 2 ── and selecting it must actually save
        scenarios.append({
            "scenario_name": f"[dependency] {flow_name} saves with the new {entity}",
            "type": "positive",
            "_generated": "relationship",
            "steps": [
                {"action": "SELECT", "selector": selector, "value": var},
                {"action": "VERIFY_VALUE", "selector": selector, "value": var},
            ],
            "expected_result": (
                f"Selecting the {entity} provided by '{master_flow}' is accepted by "
                f"{flow_name} without a validation error."
            ),
        })

        # 3 ── the exclusion rule: inactive masters must disappear
        if provide["status_field"] and provide["list_url"]:
            scenarios.append({
                "scenario_name": f"[dependency] Inactive {entity} is excluded from {flow_name}",
                "type": "negative",
                "_generated": "relationship",
                "steps": [
                    {"action": "NAVIGATE", "selector": provide["list_url"]},
                    {"action": "SELECT", "selector": provide["status_field"],
                     "value": provide["inactive_value"]},
                    {"action": "CLICK", "selector": "button:has-text('Save')"},
                    {"action": "VERIFY_MESSAGE", "value": "success"},
                    {"action": "NAVIGATE", "selector": "{{__target_url__}}"},
                    {"action": "VERIFY_OPTION_ABSENT", "selector": selector, "value": var},
                ],
                "expected_result": (
                    f"After the {entity} is set to {provide['inactive_value']}, it is no longer "
                    f"offered in {flow_name}."
                ),
            })
        else:
            notes.append(
                f"Cannot generate the inactive-exclusion check for '{entity}': the "
                f"'{master_flow}' contract needs both 'status_field' and 'list_url'."
            )

        # 4 ── referential integrity: a master in use must not vanish
        if provide["list_url"]:
            scenarios.append({
                "scenario_name": f"[dependency] {entity} in use cannot be deleted",
                "type": "negative",
                "_generated": "relationship",
                "steps": [
                    {"action": "NAVIGATE", "selector": provide["list_url"]},
                    {"action": "DIALOG", "value": "accept"},
                    {"action": "CLICK", "selector": f"tr:has-text('{var}') button.delete, "
                                                    f"tr:has-text('{var}') .delete-btn"},
                    {"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": var},
                ],
                "expected_result": (
                    f"Deleting a {entity} that {flow_name} references is blocked, or the record "
                    f"remains, rather than being removed and breaking the dependent record."
                ),
            })
        else:
            notes.append(
                f"Cannot generate the delete-in-use check for '{entity}': the "
                f"'{master_flow}' contract needs a 'list_url'."
            )

    return scenarios, notes


# ──────────────────────────────────────────────────────────────────────
# Duplicate testing — the two-phase pattern
# ──────────────────────────────────────────────────────────────────────
def duplicate_scenarios(project_name: str, flow_name: str,
                        open_steps: list[dict] | None = None,
                        submit_selector: str = "") -> tuple[list[dict], list[str]]:
    """Scenarios that genuinely create a duplicate, and one that must not.

    A duplicate test used to prove nothing. To stop repeat runs colliding, the
    platform appends a unique token, so the "duplicate" submission was unique
    too, no duplicate ever occurred, and the scenario passed having tested
    nothing. And when the application's uniqueness key is *name + date*, two
    records sharing only the name are legitimately not duplicates either.

    Both are fixed by phase 1 creating a fresh original and phase 2 repeating
    **every** field in ``unique_by`` from stored variables — which
    :func:`executor.make_dep_value_unique` now leaves untouched.

    The bonus scenario is the more interesting one: change *only* the last field
    of the key and the save must SUCCEED, proving the uniqueness key is exactly
    what the business says it is, neither narrower nor wider.
    """
    contract = load(project_name, flow_name)
    key = contract.get("unique_by") or []
    notes: list[str] = []
    if not key:
        return [], ["This flow has no 'unique_by' in its contract, so the engine cannot "
                    "know what the application treats as a duplicate. Add the selectors "
                    "that together must match, for example "
                    '["#custName", "#effectiveDate"].']

    opener = [dict(step) for step in (open_steps or [])]
    submit = submit_selector or 'button:has-text("Save")'

    def slug(selector: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", selector.lower()).strip("_") or "field"

    def phase_one() -> list[dict]:
        """Create the original and store every key field exactly as submitted."""
        steps = list(opener)
        for index, selector in enumerate(key):
            # A date field needs a date; anything else gets a tokenised name so
            # phase 1 never collides with a record left by an earlier run.
            value = ("2026-08-03" if _looks_like_date(selector)
                     else f"DUP{{{{run_token}}}}{index}" if index else "DUP{{run_token}}")
            steps.append({"action": "TYPE", "selector": selector, "value": value})
        steps.append({"action": "CLICK", "selector": submit})
        steps.append({"action": "VERIFY_MESSAGE", "value": "success"})
        for selector in key:
            steps.append({"action": "STORE", "selector": selector,
                          "value": f"dup_{slug(selector)}", "capture": "value"})
        return steps

    scenarios = [{
        "scenario_name": "A record repeating the whole uniqueness key is refused",
        "type": "negative",
        "_generated": "duplicate",
        "steps": phase_one() + opener + [
            {"action": "TYPE", "selector": selector, "value": f"{{{{dup_{slug(selector)}}}}}"}
            for selector in key
        ] + [
            {"action": "CLICK", "selector": submit},
        ],
        "expected_result": (
            "A duplicate validation message appears and no second record is created, "
            f"because {', '.join(key)} all match an existing record."
        ),
    }]

    # Only meaningful for a composite key: with one field there is nothing to vary.
    if len(key) > 1:
        varied = key[-1]
        scenarios.append({
            "scenario_name": f"Changing only {varied} is not a duplicate",
            "type": "positive",
            "_generated": "duplicate",
            "steps": phase_one() + opener + [
                {"action": "TYPE", "selector": selector,
                 "value": f"{{{{dup_{slug(selector)}}}}}"}
                for selector in key[:-1]
            ] + [
                {"action": "TYPE", "selector": varied,
                 "value": "2027-01-01" if _looks_like_date(varied) else "VARIED{{run_token}}"},
                {"action": "CLICK", "selector": submit},
            ],
            # Careful with the wording: the verdict engine classifies intent from
            # this sentence, and words like "duplicate", "refuse" or "invalid"
            # make it read as a negative expectation even in an explanatory
            # aside — which would fail a scenario that behaved correctly.
            "expected_result": (
                f"The record is saved successfully because {varied} differs from the "
                f"existing entry, so the two are separate records."
            ),
        })
    else:
        notes.append(
            f"'unique_by' has a single field ({key[0]}), so the engine cannot generate "
            f"the 'changing only one field is allowed' case. Add every field the "
            f"application really compares."
        )
    return scenarios, notes


def _looks_like_date(selector: str) -> bool:
    low = selector.lower()
    return any(word in low for word in ("date", "dob", "from", "till", "until", "effective"))
