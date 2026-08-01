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
from pathlib import Path

from . import config

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
    target.write_text(json.dumps(clean, indent=2), encoding="utf-8")
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

    return {
        "provides": provides,
        "requires": requires,
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
