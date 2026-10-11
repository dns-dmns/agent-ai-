"""Test-case sources: regenerating one never deletes another, and runs can pick."""
import os
import tempfile

os.environ["QA_WORKSPACE"] = os.environ.get("QA_WORKSPACE") or tempfile.mkdtemp(prefix="qa_src_")

from app import executor, sources  # noqa: E402


def _case(name, src=None):
    case = {"scenario_name": name, "steps": [], "expected_result": "x"}
    if src:
        case["_generated"] = src
    return case


SUITE = [_case("old gemini", "gemini"), _case("from excel", "user"),
         _case("claude one", "claude"), _case("hand written"), _case("cov", "coverage")]


def test_regenerating_gemini_keeps_every_other_source():
    out = sources.replace_source(SUITE, [_case("new gemini")], "gemini")
    names = [c["scenario_name"] for c in out]
    assert "old gemini" not in names
    assert names == ["from excel", "claude one", "hand written", "cov", "new gemini"]
    assert out[-1]["_generated"] == "gemini"


def test_untagged_cases_are_never_treated_as_gemini():
    # Older suites have no tag; a regeneration cannot prove it wrote them.
    out = sources.replace_source([_case("legacy")], [], "gemini")
    assert [c["scenario_name"] for c in out] == ["legacy"]
    assert sources.case_source(out[0]) == "manual"


def test_claude_regeneration_replaces_only_claude():
    out = sources.replace_source(SUITE, [_case("claude two")], "claude")
    assert [c["scenario_name"] for c in out if c.get("_generated") == "claude"] == ["claude two"]
    assert len(out) == len(SUITE)


def test_filter_is_strict_and_empty_means_all():
    assert len(sources.filter_by_sources(SUITE, "")) == len(SUITE)
    assert [c["scenario_name"] for c in sources.filter_by_sources(SUITE, "claude")] == ["claude one"]
    assert [c["scenario_name"] for c in sources.filter_by_sources(SUITE, "claude,user")] == \
        ["from excel", "claude one"]
    # Nothing from that source: run nothing, never silently everything.
    assert sources.filter_by_sources([_case("g", "gemini")], "claude") == []


def test_run_selection_applies_the_source_filter():
    cfg = executor.RunConfig(project_name="p", flow_name="f", sources="claude")
    picked = executor.select_scenarios([dict(c) for c in SUITE], cfg)
    assert [c["scenario_name"] for c in picked] == ["claude one"]


def test_counts_group_untagged_as_manual():
    assert sources.counts(SUITE) == {"gemini": 1, "user": 1, "claude": 1, "manual": 1, "coverage": 1}
