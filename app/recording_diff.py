"""Compare a new recording of a module with the one it would replace.

When a tester records a module again, nothing is replaced until they have seen
what changed: steps that are new, steps that are gone, and steps that target the
same control with a different value. The new recording waits in the flow's
``.pending/`` folder until the tester confirms (``/confirm-recording``).
"""
from __future__ import annotations

import difflib

#: Actions that are part of how a step was performed, not what it does.
_NOISE = {"KEYPRESS"}


def target(step: dict) -> str:
    """A human name for what a step acts on: its label, name, or selector."""
    for key in ("label", "accessible_name"):
        value = str(step.get(key) or "").strip()
        if value:
            return value[:80]
    sel = str(step.get("selector") or "").strip()
    return sel[:80]


def brief(step: dict) -> dict:
    return {"action": step.get("action", ""), "target": target(step),
            "value": str(step.get("value") or "")[:80],
            "selector": str(step.get("selector") or "")[:120]}


def signature(step: dict) -> tuple[str, str]:
    """What a step does, ignoring the value typed: matched across recordings."""
    return (str(step.get("action") or ""), str(step.get("selector") or target(step)))


def compare(old: list[dict], new: list[dict]) -> dict:
    """Side-by-side rows plus a field-level summary.

    Row status: ``same`` | ``changed`` (same control, different value) |
    ``added`` | ``removed``.
    """
    old = [s for s in old or [] if isinstance(s, dict) and s.get("action") not in _NOISE]
    new = [s for s in new or [] if isinstance(s, dict) and s.get("action") not in _NOISE]
    rows: list[dict] = []
    matcher = difflib.SequenceMatcher(a=[signature(s) for s in old],
                                      b=[signature(s) for s in new], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for a, b in zip(old[i1:i2], new[j1:j2]):
                same = str(a.get("value") or "") == str(b.get("value") or "")
                rows.append({"status": "same" if same else "changed", "old": brief(a), "new": brief(b)})
            continue
        left, right = old[i1:i2], new[j1:j2]
        paired = 0
        if op == "replace":
            for a, b in zip(left, right):
                if a.get("action") == b.get("action") and target(a) == target(b):
                    rows.append({"status": "changed", "old": brief(a), "new": brief(b)})
                    paired += 1
                else:
                    break
        for a in left[paired:]:
            rows.append({"status": "removed", "old": brief(a), "new": None})
        for b in right[paired:]:
            rows.append({"status": "added", "old": None, "new": brief(b)})

    def fields(steps):
        return {target(s) for s in steps
                if s.get("action") in ("TYPE", "SELECT", "CHECK", "UNCHECK", "UPLOAD") and target(s)}

    old_fields, new_fields = fields(old), fields(new)
    counts = {k: sum(1 for r in rows if r["status"] == k)
              for k in ("same", "changed", "added", "removed")}
    return {
        "rows": rows,
        "counts": counts,
        "old_steps": len(old), "new_steps": len(new),
        "fields_added": sorted(new_fields - old_fields),
        "fields_removed": sorted(old_fields - new_fields),
    }
