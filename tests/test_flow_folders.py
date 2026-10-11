"""Flows in folders: "Master/Item Master" mirrors the application's menu."""
import os
import sys
import tempfile
import asyncio

os.environ["QA_WORKSPACE"] = os.environ.get("QA_WORKSPACE") or tempfile.mkdtemp(prefix="qa_folders_")
os.environ.setdefault("QA_SCHEDULER", "0")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import config, main  # noqa: E402

PROJECT = "FolderTest"


def _flow(name, marker="flow.json"):
    d = config.flow_dir(PROJECT, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / marker).write_text("{}", encoding="utf-8")
    (d / "screenshots").mkdir(exist_ok=True)       # evidence must not look like a flow


def test_flows_are_found_inside_folders():
    _flow("Login")
    _flow("Master/Item Master")
    _flow("Master/RM Item Master", "actions.json")
    _flow("Sales Order/Create Order", "test_cases.json")
    (config.flows_dir(PROJECT) / "Empty Folder").mkdir(parents=True, exist_ok=True)
    assert main.flow_names(PROJECT) == [
        "Login", "Master/Item Master", "Master/RM Item Master", "Sales Order/Create Order"]


@pytest.mark.parametrize("name", ["Master/Item Master", "Login", "A/B/C"])
def test_folder_paths_are_accepted(name):
    main.validate_safe_path(name)


@pytest.mark.parametrize("name", ["../x", "Master/../..", "/abs", "Master/", "a//b",
                                  "C:/Windows", "a" + chr(92) + "b", ".hidden",
                                  "Master/.git", " Master/x"])
def test_escapes_and_odd_paths_are_refused(name):
    with pytest.raises(HTTPException):
        main.validate_safe_path(name)
