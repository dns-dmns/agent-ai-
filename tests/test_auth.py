"""The API-key middleware: who gets in, and how the key may travel."""
import asyncio
import os
import sys
import tempfile

os.environ["QA_WORKSPACE"] = os.environ.get("QA_WORKSPACE") or tempfile.mkdtemp(prefix="qa_auth_")
os.environ.setdefault("QA_SCHEDULER", "0")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import pytest  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402

from app import config, main  # noqa: E402


def _request(path="/list-projects", client="127.0.0.1", headers=None, query=""):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": path, "query_string": query.encode(),
             "headers": raw_headers, "client": (client, 50000), "server": ("127.0.0.1", 8000),
             "scheme": "http", "root_path": ""}
    return Request(scope)


def _status(request) -> int:
    async def call_next(_req):
        return PlainTextResponse("ok")
    return asyncio.run(main.api_key_guard(request, call_next)).status_code


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "ALLOW_REMOTE_WITHOUT_KEY", False)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "s3cret")


def test_no_key_serves_this_machine(no_key):
    assert _status(_request(client="127.0.0.1")) == 200
    assert _status(_request(client="::1")) == 200


def test_no_key_refuses_other_machines(no_key):
    assert _status(_request(client="192.168.1.20")) == 403
    assert _status(_request(client="172.17.0.1")) == 403      # Docker bridge


def test_remote_without_key_is_an_explicit_opt_in(no_key, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_REMOTE_WITHOUT_KEY", True)
    assert _status(_request(client="192.168.1.20")) == 200


def test_key_in_header_or_cookie_is_accepted(with_key):
    assert _status(_request(client="10.0.0.9", headers={"X-API-Key": "s3cret"})) == 200
    assert _status(_request(client="10.0.0.9", headers={"Cookie": "qa_api_key=s3cret"})) == 200


def test_wrong_or_missing_key_is_refused(with_key):
    assert _status(_request(client="127.0.0.1")) == 401
    assert _status(_request(client="10.0.0.9", headers={"X-API-Key": "nope"})) == 401


def test_key_in_the_url_is_refused_even_when_correct(with_key):
    assert _status(_request(client="10.0.0.9", query="api_key=s3cret")) == 401


def test_public_paths_stay_open(with_key):
    assert _status(_request(path="/health", client="10.0.0.9")) == 200
