"""Where a test case came from, and keeping each source's cases apart.

A suite is shared by several writers: Gemini's multi-agent generation, test
cases Claude writes from a recording, the CSV/worksheet upload, coverage
enumeration, the defect importer, rules and contracts. Each tags its cases with
``_generated``. Two things follow:

- **Regenerating one source never deletes another.** Gemini generation used to
  overwrite the whole ``test_cases.json``, silently wiping every uploaded,
  enumerated and hand-written case. ``replace_source`` swaps out only the cases
  that source owns.
- **A run can pick its sources.** ``filter_by_sources`` is what the Run screen's
  "Test source" filter and ``/trigger-run?sources=`` use.
"""
from __future__ import annotations

#: tag -> label shown in the UI. Order is the order of the filter menu.
SOURCES: dict[str, str] = {
    "claude": "Claude",
    "gemini": "Gemini (API)",
    "user": "Excel / CSV",
    "coverage": "Coverage",
    "defect": "Defect log",
    "rule": "Rules",
    "relationship": "Dependencies",
    "duplicate": "Duplicate checks",
    "manual": "Manual / older",
}

#: Cases written before tagging existed carry no ``_generated``. They are not
#: assumed to be Gemini's -- a regeneration must never delete what it cannot
#: prove it wrote.
UNTAGGED = "manual"


def case_source(case: dict) -> str:
    tag = str((case or {}).get("_generated") or "").strip().lower()
    return tag if tag in SOURCES else UNTAGGED


def tag(cases: list[dict], source: str) -> list[dict]:
    for case in cases:
        if isinstance(case, dict):
            case["_generated"] = source
    return cases


def replace_source(existing: list[dict], incoming: list[dict], source: str) -> list[dict]:
    """``existing`` minus ``source``'s old cases, plus ``incoming`` tagged as ``source``.

    Other sources keep their cases and their order; the new cases go at the end.
    """
    kept = [c for c in existing or [] if case_source(c) != source]
    return kept + tag(list(incoming or []), source)


def parse(selection: str) -> set[str]:
    """``"claude, gemini"`` -> ``{"claude", "gemini"}``; unknown names are dropped."""
    return {s.strip().lower() for s in (selection or "").split(",")
            if s.strip().lower() in SOURCES}


def filter_by_sources(cases: list[dict], selection: str) -> list[dict]:
    """Only the cases from the selected sources. An empty selection means all.

    Unlike the type filter, an empty result stays empty: running everything
    when the user asked for Claude's cases would report results for tests they
    did not choose.
    """
    wanted = parse(selection)
    if not wanted:
        return list(cases)
    return [c for c in cases if case_source(c) in wanted]


def counts(cases: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for case in cases or []:
        key = case_source(case)
        out[key] = out.get(key, 0) + 1
    return out
