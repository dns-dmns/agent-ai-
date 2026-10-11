"""Re-recording a module: compare, then replace or discard -- never two copies."""
import asyncio
import json
import os
import sys
import tempfile

os.environ["QA_WORKSPACE"] = os.environ.get("QA_WORKSPACE") or tempfile.mkdtemp(prefix="qa_review_")
os.environ.setdefault("QA_SCHEDULER", "0")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402

from app import config, main, recording_diff  # noqa: E402

OLD = [{"action": "NAVIGATE", "selector": "https://x/list"},
       {"action": "CLICK", "selector": "#add", "label": "Add"},
       {"action": "TYPE", "selector": "#name", "label": "Name", "value": "Alpha"},
       {"action": "TYPE", "selector": "#code", "label": "Code", "value": "A1"},
       {"action": "CLICK", "selector": "#save", "label": "Save"}]
NEW = [{"action": "NAVIGATE", "selector": "https://x/list"},
       {"action": "CLICK", "selector": "#add", "label": "Add"},
       {"action": "TYPE", "selector": "#name", "label": "Name", "value": "Beta"},
       {"action": "KEYPRESS", "selector": "#name", "value": "Tab"},
       {"action": "TYPE", "selector": "#gst", "label": "GST", "value": "29AB"},
       {"action": "CLICK", "selector": "#save", "label": "Save"}]


def test_compare_marks_added_removed_changed():
    d = recording_diff.compare(OLD, NEW)
    by = {(r["status"], (r["old"] or r["new"])["target"]) for r in d["rows"]}
    assert ("changed", "Name") in by        # same field, new value
    assert ("removed", "Code") in by
    assert ("added", "GST") in by
    assert d["counts"] == {"same": 3, "changed": 1, "added": 1, "removed": 1}
    assert d["fields_added"] == ["GST"] and d["fields_removed"] == ["Code"]
    assert d["new_steps"] == 5               # the commit-Tab is not a step worth reviewing


def _flow(name):
    flow = config.flow_dir("Rev", name)
    (flow / ".pending" / "screenshots").mkdir(parents=True, exist_ok=True)
    (flow / "actions.json").write_text(json.dumps(OLD), encoding="utf-8")
    (flow / "flow.json").write_text(json.dumps({"flow_name": name, "source": "explorer",
                                                "target_url": "https://x/list"}), encoding="utf-8")
    (flow / "test_cases.json").write_text("[]", encoding="utf-8")
    (flow / ".pending" / "actions.json").write_text(json.dumps(NEW), encoding="utf-8")
    (flow / ".pending" / "flow.json").write_text(json.dumps({"flow_name": name, "step_count": 6,
                                                             "target_url": "https://x/list"}), encoding="utf-8")
    return flow


def test_replace_leaves_exactly_one_recording():
    flow = _flow("Master/Thing")
    out = asyncio.run(main.confirm_recording("Master/Thing", "replace", project_name="Rev"))
    assert out["status"] == "success"
    assert json.loads((flow / "actions.json").read_text(encoding="utf-8")) == NEW
    assert not (flow / ".pending").exists()
    meta = json.loads((flow / "flow.json").read_text(encoding="utf-8"))
    assert "source" not in meta                       # a person's recording now
    assert (flow / "test_cases.json").exists()         # suite untouched


def test_discard_keeps_the_old_recording():
    flow = _flow("Master/Other")
    asyncio.run(main.confirm_recording("Master/Other", "discard", project_name="Rev"))
    assert json.loads((flow / "actions.json").read_text(encoding="utf-8")) == OLD
    assert not (flow / ".pending").exists()


def test_pending_folder_is_not_listed_as_a_flow():
    _flow("Master/Listed")
    assert not any(".pending" in n for n in main.flow_names("Rev"))


def _status(path):
    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000), "scheme": "http",
             "root_path": ""}

    async def call_next(_req):
        return PlainTextResponse("ok")
    return asyncio.run(main.private_files_and_fresh_assets(Request(scope), call_next)).status_code


def test_secrets_are_not_served_but_evidence_is():
    for secret in ("/static/projects/Liberty/login_data.json", "/static/projects/Liberty/config.json",
                   "/static/projects/Liberty/session.json", "/static/projects/Liberty/session.admin.json"):
        assert _status(secret) == 404, secret
    for ok in ("/static/projects/Liberty/flows/item/screenshots/step_1.png",
               "/static/projects/Liberty/flows/item/report.json"):
        assert _status(ok) == 200, ok


def test_stop_with_review_keeps_old_until_confirmed(monkeypatch):
    flow = config.flow_dir("Rev", "Master/Stop")
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "actions.json").write_text(json.dumps(OLD), encoding="utf-8")
    (flow / "flow.json").write_text(json.dumps({"flow_name": "Master/Stop"}), encoding="utf-8")

    async def no_browser():
        return None
    monkeypatch.setattr(main, "close_active_browser", no_browser)
    monkeypatch.setitem(main.active_session, "page", object())
    monkeypatch.setitem(main.active_session, "flow_name", "Master/Stop")
    monkeypatch.setitem(main.active_session, "url", "https://x/list")
    monkeypatch.setitem(main.active_session, "project_name", "Rev")
    monkeypatch.setitem(main.active_session, "actions", [dict(a) for a in NEW])
    monkeypatch.setitem(main.active_session, "start_time", 0)

    out = asyncio.run(main.stop_recording_api(review=True))
    assert out["status"] == "review"
    assert out["diff"]["counts"]["added"] == 1
    # nothing replaced yet: old recording in place, new one waiting
    assert json.loads((flow / "actions.json").read_text(encoding="utf-8")) == OLD
    assert (flow / ".pending" / "actions.json").exists()
