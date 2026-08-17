"""Multi-Project Enterprise AI QA Automation Platform — FastAPI application.

Responsibilities kept in this file: HTTP surface, browser-recording session and
AI generation orchestration. The Playwright execution engine lives in
:mod:`executor`, prompts in :mod:`ai_agents`, LLM plumbing in :mod:`llm`,
verdicts in :mod:`verdict`, history in :mod:`db`, paths in :mod:`config`.
"""

import sys
import asyncio

# Force Windows to use the Proactor event loop so Playwright can spawn browsers.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import csv
import glob
import io
import json
import mimetypes
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright

from . import ai_agents
from . import api_seed
from . import config
from . import contracts
from . import coverage
from . import db
from . import defects
from . import executor
from . import export_project
from . import learned
from . import llm
from . import recorder
from . import rules
from . import scheduler

mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/mp4", ".mp4")

log = config.get_logger("qa_agent")

app = FastAPI(title="AI QA Automation Platform", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Only the projects folder is web-exposed — never the workspace root, which
# holds .env, session.json and the source files.
config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    "/static/projects",
    StaticFiles(directory=str(config.PROJECTS_DIR)),
    name="static",
)

# UI assets (stylesheet + scripts). Separate from /static/projects so test
# evidence and application code never share a mount.
_web_dir = config.web_dir()
if _web_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(_web_dir)), name="assets")

PUBLIC_PATHS = ("/", "/legacy", "/health", "/favicon.ico", "/docs", "/openapi.json", "/redoc")


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """Optional shared-secret auth. Disabled entirely when QA_API_KEY is unset."""
    if not config.API_KEY:
        return await call_next(request)
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    supplied = (
        request.headers.get("X-API-Key")
        or request.query_params.get("api_key")
        or request.cookies.get("qa_api_key")
        or ""
    )
    if supplied != config.API_KEY:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key. Set it in the dashboard header."},
        )
    return await call_next(request)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def validate_safe_path(*names: str) -> None:
    """Prevent directory traversal through user-supplied path segments.

    Empty names are skipped because several parameters (``auth_flow``,
    ``env_name`` …) are legitimately optional. Anything that *must* name a real
    folder has to go through :func:`require_name` as well — an empty string
    resolves to the parent directory, which is how a delete endpoint could
    otherwise be pointed at the whole workspace.
    """
    for name in names:
        if not name:
            continue
        if ".." in name or "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="Invalid path parameter detected.")


def require_name(**named: str) -> None:
    """Reject blank values for parameters that must identify a real folder."""
    for label, value in named.items():
        if not (value or "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"'{label.replace('_', ' ')}' is required and cannot be empty.",
            )


def assert_inside_projects(path: Path, label: str = "target") -> Path:
    """Refuse to operate on the projects root or anything outside it."""
    resolved = path.resolve()
    root = config.PROJECTS_DIR.resolve()
    if resolved == root or root not in resolved.parents:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to modify {label}: '{resolved}' is not a single project "
                   f"or flow folder inside the workspace.",
        )
    return resolved


def no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    """Atomic write so a crash or a concurrent reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=4)
    os.replace(tmp, path)


def sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, default=str) + "\n\n"


def require_flow(project_name: str, flow_name: str) -> Path:
    require_name(project_name=project_name, flow_name=flow_name)
    validate_safe_path(project_name, flow_name)
    path = config.flow_dir(project_name, flow_name)
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="Flow not found.")
    return path


def require_project(project_name: str) -> Path:
    require_name(project_name=project_name)
    validate_safe_path(project_name)
    path = config.project_dir(project_name)
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="Project not found.")
    return path


def move_to_trash(source: Path, label: str) -> Path:
    """Soft delete — nothing is ever hard-deleted from the workspace.

    Guarded twice: the source must be a single project or flow folder strictly
    inside ``projects/``, so no combination of parameters can send the whole
    workspace to the trash.
    """
    assert_inside_projects(source, label)
    target = config.TRASH_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    log.warning("Moved %s to trash at %s", source, target)
    return target


# ──────────────────────────────────────────────────────────────────────
# Recording session
# ──────────────────────────────────────────────────────────────────────
active_session: dict[str, Any] = {
    "playwright": None,
    "browser": None,
    "context": None,
    "page": None,
    "recording": False,
    "actions": [],
    "flow_name": None,
    "url": None,
    "project_name": config.DEFAULT_PROJECT,
    "start_time": 0,
    "binding_ready": False,
}
_recording_lock = asyncio.Lock()


async def close_active_browser() -> None:
    for key in ("browser", "playwright"):
        obj = active_session.get(key)
        if not obj:
            continue
        try:
            await (obj.close() if key == "browser" else obj.stop())
        except Exception:
            pass
    active_session.update({
        "playwright": None, "browser": None, "context": None, "page": None,
        "recording": False, "binding_ready": False,
    })


# ──────────────────────────────────────────────────────────────────────
# Dashboard & health
# ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    dashboard = config.dashboard_file()
    if not dashboard.exists():
        raise HTTPException(
            status_code=500,
            detail=f"dashboard.html not found (looked for {dashboard}).",
        )
    return dashboard.read_text(encoding="utf-8")


@app.get("/legacy", response_class=HTMLResponse)
async def get_legacy_dashboard():
    """The pre-redesign single-file UI, kept reachable as a fallback."""
    legacy = config.web_dir() / "legacy" / "dashboard.legacy.html"
    if not legacy.exists():
        raise HTTPException(status_code=404, detail="The legacy dashboard is not installed.")
    return legacy.read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "workspace": str(config.BASE_DIR),
        "projects": len([p for p in config.PROJECTS_DIR.iterdir() if p.is_dir()])
        if config.PROJECTS_DIR.exists() else 0,
        "ai_configured": bool(config.GEMINI_API_KEY),
        "auth_enabled": bool(config.API_KEY),
        "active_runs": sum(1 for r in executor.RUN_REGISTRY.values() if r.status == "running"),
        "model": config.GEMINI_MODEL,
    }


@app.get("/platform-info")
async def platform_info(response: Response):
    no_cache(response)
    return {
        "workspace": str(config.BASE_DIR),
        "ai_configured": bool(config.GEMINI_API_KEY),
        "model": config.GEMINI_MODEL,
        "auth_enabled": bool(config.API_KEY),
        "max_workers": config.MAX_PARALLEL_WORKERS,
        "vision_healing": config.VISION_HEALING_ENABLED,
        "llm_usage": llm.usage_summary(),
        "db": db.stats(),
        "scheduler": scheduler.is_running(),
    }


@app.get("/session-status")
async def get_session_status(project_name: str = ""):
    if project_name:
        validate_safe_path(project_name)
        return {"active": config.project_session_file(project_name).exists()}
    return {"active": config.GLOBAL_SESSION_FILE.exists()}


# ──────────────────────────────────────────────────────────────────────
# Projects
# ──────────────────────────────────────────────────────────────────────
@app.get("/list-projects")
async def list_projects_api(response: Response):
    no_cache(response)
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    projects = sorted(p.name for p in config.PROJECTS_DIR.iterdir() if p.is_dir()
                      and not p.name.startswith("."))
    if not projects:
        config.flows_dir(config.DEFAULT_PROJECT).mkdir(parents=True, exist_ok=True)
        projects = [config.DEFAULT_PROJECT]
    return {"projects": projects}


@app.post("/create-project")
async def create_project_api(project_name: str):
    validate_safe_path(project_name)
    if not project_name.strip():
        raise HTTPException(status_code=400, detail="Project name cannot be empty.")
    config.flows_dir(project_name.strip()).mkdir(parents=True, exist_ok=True)
    db.audit("create_project", project_name)
    return {"status": "success", "message": f"Project '{project_name}' created successfully."}


@app.post("/delete-project")
async def delete_project_api(project_name: str):
    path = require_project(project_name)
    moved = move_to_trash(path, f"project_{project_name}")
    db.audit("delete_project", project_name, details={"trash": str(moved)})
    return {
        "status": "success",
        "message": f"Project '{project_name}' moved to .trash (recoverable at {moved.name}).",
    }


@app.post("/rename-project")
async def rename_project_api(project_name: str, new_name: str):
    source = require_project(project_name)
    require_name(new_name=new_name)
    validate_safe_path(new_name)
    target = config.project_dir(new_name.strip())
    if target.exists():
        raise HTTPException(status_code=400, detail="A project with that name already exists.")
    assert_inside_projects(source, "project")
    assert_inside_projects(target, "project")
    shutil.move(str(source), str(target))
    db.audit("rename_project", project_name, details={"new_name": new_name})
    return {"status": "success", "message": f"Project renamed to '{new_name}'."}


@app.get("/get-project-config")
async def get_project_config(project_name: str, response: Response):
    validate_safe_path(project_name)
    no_cache(response)
    cfg = read_json(config.project_config_file(project_name), {}) or {}
    creds = dict(cfg.get("credentials") or {})
    if creds.get("password"):
        creds["password"] = "********"          # never echo secrets back
    legacy = read_json(config.GLOBAL_LOGIN_DATA_FILE, {}) or {}
    roles = {}
    for role_name, role_creds in (cfg.get("roles") or {}).items():
        masked = dict(role_creds or {})
        if masked.get("password"):
            masked["password"] = "********"
        roles[role_name] = masked
    return {
        "environments": cfg.get("environments") or {},
        "active_env": cfg.get("active_env", ""),
        "credentials": creds,
        "has_credentials": bool((cfg.get("credentials") or {}).get("username")
                                or legacy.get("username")),
        "defaults": cfg.get("defaults") or {},
        "notifications": cfg.get("notifications") or {},
        "roles": roles,
    }


@app.post("/save-project-config")
async def save_project_config(project_name: str, payload: dict = Body(...)):
    validate_safe_path(project_name)
    path = config.project_config_file(project_name)
    current = read_json(path, {}) or {}

    for key in ("environments", "active_env", "defaults", "notifications"):
        if key in payload:
            current[key] = payload[key]

    incoming_creds = payload.get("credentials")
    if isinstance(incoming_creds, dict):
        creds = dict(current.get("credentials") or {})
        for key, value in incoming_creds.items():
            if value == "********":              # unchanged masked field
                continue
            creds[key] = value
        current["credentials"] = creds

    # Per-role logins for SWITCH_ROLE: a creator, an approver and an operator are
    # different people, and a workflow with an approval gate cannot be tested
    # honestly with one account.
    incoming_roles = payload.get("roles")
    if isinstance(incoming_roles, dict):
        roles = dict(current.get("roles") or {})
        for role_name, role_creds in incoming_roles.items():
            if not isinstance(role_creds, dict):
                continue
            if role_creds.get("_delete"):
                roles.pop(role_name, None)
                continue
            existing = dict(roles.get(role_name) or {})
            for key, value in role_creds.items():
                if value == "********":          # unchanged masked field
                    continue
                existing[key] = value
            if existing.get("username"):
                roles[role_name] = existing
        current["roles"] = roles

    config.project_dir(project_name).mkdir(parents=True, exist_ok=True)
    write_json(path, current)
    db.audit("save_project_config", project_name)
    return {"status": "success", "message": "Project settings saved."}


# ──────────────────────────────────────────────────────────────────────
# Flows
# ──────────────────────────────────────────────────────────────────────
@app.get("/list-flows")
async def list_flows(response: Response, project_name: str = config.DEFAULT_PROJECT):
    validate_safe_path(project_name)
    no_cache(response)
    flows_root = config.flows_dir(project_name)
    if not flows_root.exists():
        return {"auth_flows": [], "business_flows": []}

    auth_flows, business_flows = [], []
    for folder in sorted(p.name for p in flows_root.iterdir() if p.is_dir()):
        (auth_flows if executor.is_auth_flow(folder) else business_flows).append(folder)
    return {"auth_flows": auth_flows, "business_flows": business_flows}


@app.get("/get-flow-details")
async def get_flow_details(flow_name: str, response: Response,
                           project_name: str = config.DEFAULT_PROJECT):
    flow_path = require_flow(project_name, flow_name)
    no_cache(response)

    recording = flow_path / "recording.webm"
    details: dict[str, Any] = {
        "flow": read_json(flow_path / "flow.json", {}) or {},
        "actions": read_json(flow_path / "actions.json", []) or [],
        "has_test_cases": (flow_path / "test_cases.json").exists(),
        "has_scripts": (flow_path / "scripts.json").exists(),
        "has_test_data": (flow_path / "test_data.json").exists(),
        "has_documentation": (flow_path / "documentation.md").exists(),
        "has_suggestions": (flow_path / "suggestions.json").exists(),
        "has_spec": (flow_path / "spec.json").exists(),
        "has_recording": recording.exists(),
        "recording_url": config.static_url(project_name, flow_name, "recording.webm")
        if recording.exists() else "",
    }

    if details["has_test_cases"]:
        details["test_cases"] = read_json(flow_path / "test_cases.json", []) or []
    if details["has_scripts"]:
        details["scripts"] = read_json(flow_path / "scripts.json", {}) or {}
    if details["has_test_data"]:
        details["test_data"] = read_json(flow_path / "test_data.json", {}) or {}
    if details["has_documentation"]:
        details["documentation"] = (flow_path / "documentation.md").read_text(encoding="utf-8")
    if details["has_suggestions"]:
        details["suggestions"] = read_json(flow_path / "suggestions.json", {}) or {}
    if details["has_spec"]:
        details["spec"] = read_json(flow_path / "spec.json", {}) or {}

    report_path = flow_path / "report.json"
    details["has_report"] = report_path.exists()
    details["latest_report"] = read_json(report_path, None) if report_path.exists() else None
    details["run_history"] = (read_json(flow_path / "run_history.json", []) or [])[-30:]
    details["data_files"] = sorted(
        p.name for p in (flow_path / "data").glob("*.csv")
    ) if (flow_path / "data").exists() else []
    return details


@app.post("/create-empty-flow")
async def create_empty_flow_api(flow_name: str, url: str,
                                project_name: str = config.DEFAULT_PROJECT):
    validate_safe_path(flow_name, project_name)
    if not flow_name.strip():
        raise HTTPException(status_code=400, detail="Flow name cannot be empty.")
    if not url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty.")

    flow_path = config.flow_dir(project_name, flow_name.strip())
    (flow_path / "screenshots").mkdir(parents=True, exist_ok=True)
    write_json(flow_path / "flow.json", {
        "flow_name": flow_name.strip(),
        "target_url": url.strip(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": 0,
        "step_count": 0,
    })
    if not (flow_path / "actions.json").exists():
        write_json(flow_path / "actions.json", [])
    db.audit("create_flow", f"{project_name}/{flow_name}")
    return {"status": "success",
            "message": f"Empty flow module '{flow_name}' registered under '{project_name}'."}


@app.post("/delete-flow")
async def delete_flow_api(flow_name: str, project_name: str = config.DEFAULT_PROJECT):
    flow_path = require_flow(project_name, flow_name)
    moved = move_to_trash(flow_path, f"{project_name}_{flow_name}")
    db.audit("delete_flow", f"{project_name}/{flow_name}", details={"trash": str(moved)})
    return {"status": "success",
            "message": f"Flow '{flow_name}' moved to .trash (recoverable at {moved.name})."}


@app.post("/rename-flow")
async def rename_flow_api(flow_name: str, new_name: str,
                          project_name: str = config.DEFAULT_PROJECT):
    flow_path = require_flow(project_name, flow_name)
    require_name(new_name=new_name)
    validate_safe_path(new_name)
    target = config.flow_dir(project_name, new_name.strip())
    if target.exists():
        raise HTTPException(status_code=400, detail="A flow with that name already exists.")
    assert_inside_projects(flow_path, "flow")
    assert_inside_projects(target, "flow")
    shutil.move(str(flow_path), str(target))

    meta = read_json(target / "flow.json", {}) or {}
    meta["flow_name"] = new_name.strip()
    write_json(target / "flow.json", meta)
    db.audit("rename_flow", f"{project_name}/{flow_name}", details={"new_name": new_name})
    return {"status": "success", "message": f"Flow renamed to '{new_name}'."}


@app.post("/duplicate-flow")
async def duplicate_flow_api(flow_name: str, new_name: str,
                             project_name: str = config.DEFAULT_PROJECT):
    flow_path = require_flow(project_name, flow_name)
    require_name(new_name=new_name)
    validate_safe_path(new_name)
    target = config.flow_dir(project_name, new_name.strip())
    if target.exists():
        raise HTTPException(status_code=400, detail="A flow with that name already exists.")
    assert_inside_projects(target, "flow")
    shutil.copytree(
        flow_path, target,
        ignore=shutil.ignore_patterns("videos", "traces", "reports", "downloads", "baseline"),
    )
    meta = read_json(target / "flow.json", {}) or {}
    meta["flow_name"] = new_name.strip()
    write_json(target / "flow.json", meta)
    return {"status": "success", "message": f"Flow duplicated as '{new_name}'."}


# ──────────────────────────────────────────────────────────────────────
# Recording (Tab 1)
# ──────────────────────────────────────────────────────────────────────
@app.post("/create-flow")
async def create_flow_api(flow_name: str, url: str,
                          project_name: str = config.DEFAULT_PROJECT):
    validate_safe_path(flow_name, project_name)
    if not flow_name.strip():
        raise HTTPException(status_code=400, detail="Flow name cannot be empty.")
    if not url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty.")

    async with _recording_lock:
        await close_active_browser()
        try:
            active_session["playwright"] = await async_playwright().start()
            active_session["browser"] = await active_session["playwright"].chromium.launch(
                headless=False, args=["--start-maximized"]
            )

            flow_path = config.flow_dir(project_name, flow_name.strip())
            flow_path.mkdir(parents=True, exist_ok=True)

            context_args: dict[str, Any] = {
                "no_viewport": True,
                "accept_downloads": True,
                "record_video_dir": str(flow_path),
                "record_video_size": {"width": 1280, "height": 720},
            }
            session_file = config.project_session_file(project_name)
            if session_file.exists():
                context_args["storage_state"] = str(session_file)

            active_session["context"] = await active_session["browser"].new_context(**context_args)
            active_session["page"] = await active_session["context"].new_page()
            active_session.update({
                "flow_name": flow_name.strip(),
                "url": url.strip(),
                "project_name": project_name,
                "recording": False,
                "actions": [],
                "start_time": time.time(),
                "binding_ready": False,
            })

            await active_session["page"].goto(
                url, wait_until="domcontentloaded", timeout=60000
            )
            return {"status": "success",
                    "message": f"Browser opened at {url}. Click 'Start Recording' to begin."}
        except Exception as err:
            await close_active_browser()
            raise HTTPException(status_code=500, detail=str(err)) from err


async def handle_exposed_event(event: dict) -> None:
    """Persist one recorded interaction plus a step screenshot.

    An ``__EFFECT__`` event is not a step — it describes what the *previous* step
    caused, so it is attached to that action rather than appended. A recording
    then carries the message the application really showed, which is what lets a
    generated scenario assert real wording instead of invented wording.
    """
    if not active_session["recording"]:
        return

    if (event.get("action") or "") == "__EFFECT__":
        actions = active_session["actions"]
        target = None
        # The most recent action with a matching selector, else simply the last.
        for candidate in reversed(actions):
            if candidate.get("selector") == event.get("selector"):
                target = candidate
                break
        if target is None and actions:
            target = actions[-1]
        if target is not None:
            target["effect"] = event.get("effect") or {}
        return

    # Required-ness is evaluated at event time, and the strongest signal on an
    # Angular form (ng-invalid on an empty control) only holds while the field IS
    # empty. So the same field arrived required=True on CLICK and required=False
    # on the TYPE that filled it, and whichever a consumer read last won —
    # generation was told a mandatory field was optional. Carry the True forward:
    # a field does not stop being mandatory because it is now filled. Mirrors
    # learned.remember_fields, which already ORs the same flag across runs.
    selector = event.get("selector")
    if selector and event.get("required") is not True:
        for earlier in active_session["actions"]:
            if earlier.get("selector") == selector and earlier.get("required") is True:
                event["required"] = True
                break

    active_session["actions"].append(event)
    step_num = len(active_session["actions"])
    temp_path = config.TEMP_DIR / f"rec_{int(active_session['start_time'])}_step_{step_num}.png"
    try:
        await active_session["page"].screenshot(path=str(temp_path))
        event["screenshot"] = str(temp_path)
    except Exception:
        event["screenshot"] = None


@app.post("/start-recording")
async def start_recording_api():
    if not active_session["page"]:
        raise HTTPException(status_code=400,
                            detail="No active browser session found. Launch the browser first!")
    try:
        page = active_session["page"]
        context = active_session["context"]

        if not active_session["binding_ready"]:
            try:
                await page.expose_function(
                    "recordPlaywrightEvent",
                    lambda ev: asyncio.create_task(handle_exposed_event(ev)),
                )
            except Exception as err:
                # Already registered (user pressed Start twice) — harmless.
                if "already registered" not in str(err).lower():
                    raise
            active_session["binding_ready"] = True
            await context.add_init_script(recorder.JS_RECORDER_SCRIPT)

        active_session["recording"] = True
        await page.evaluate(recorder.JS_RECORDER_SCRIPT)
        return {"status": "success",
                "message": "Recording started. Interact with the browser window."}
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err)) from err


@app.post("/stop-recording")
async def stop_recording_api(force: bool = False):
    if not active_session["page"]:
        raise HTTPException(status_code=400, detail="No active recording found.")

    flow_name = active_session["flow_name"]
    url = active_session["url"]
    project_name = active_session["project_name"]
    actions = list(active_session["actions"])
    duration = int(time.time() - active_session["start_time"])
    flow_path = config.flow_dir(project_name, flow_name)
    existing_actions = read_json(flow_path / "actions.json", []) or []

    # Guard: never wipe a good recording with an empty one (e.g. the user forgot
    # to press Start Recording).
    if not actions and existing_actions and not force:
        await close_active_browser()
        raise HTTPException(
            status_code=409,
            detail=(
                f"No interactions were captured, but '{flow_name}' already has "
                f"{len(existing_actions)} recorded steps. The existing recording was "
                "kept. Did you forget to press 'Start Recording'? Re-run with "
                "force=true to overwrite it deliberately."
            ),
        )

    try:
        await close_active_browser()

        # Promote the newest webm to recording.webm.
        webm_files = [
            Path(p) for p in glob.glob(str(flow_path / "*.webm"))
            if Path(p).name != "recording.webm"
        ]
        if webm_files:
            webm_files.sort(key=lambda p: p.stat().st_mtime)
            latest = webm_files[-1]
            target = flow_path / "recording.webm"
            try:
                if target.exists():
                    target.unlink()
                shutil.move(str(latest), str(target))
            except Exception as err:
                log.warning("Could not move the recording video: %s", err)
            for leftover in webm_files[:-1]:
                try:
                    leftover.unlink()
                except Exception:
                    pass

        shots = flow_path / "screenshots"
        shutil.rmtree(shots, ignore_errors=True)
        shots.mkdir(parents=True, exist_ok=True)

        for idx, act in enumerate(actions, start=1):
            temp_img = act.get("screenshot")
            if temp_img and os.path.exists(temp_img):
                target_img = shots / f"step_{idx}.png"
                shutil.move(temp_img, target_img)
                act["screenshot"] = config.static_url(
                    project_name, flow_name, "screenshots", target_img.name
                )

        write_json(flow_path / "flow.json", {
            "flow_name": flow_name,
            "target_url": url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": duration,
            "step_count": len(actions),
        })
        write_json(flow_path / "actions.json", actions)
        db.audit("stop_recording", f"{project_name}/{flow_name}",
                 details={"steps": len(actions)})

        return {"status": "success",
                "message": f"Recording saved ({len(actions)} steps) to "
                           f"projects/{project_name}/flows/{flow_name}/"}
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err)) from err


# ──────────────────────────────────────────────────────────────────────
# AI generation (Tab 2)
# ──────────────────────────────────────────────────────────────────────
LOGIN_URL_MARKERS = ("login", "signin", "sign-in", "auth", "logon", "session")


def _is_login_url(url: str) -> bool:
    return any(marker in (url or "").lower() for marker in LOGIN_URL_MARKERS)


def recording_grounding(actions: list) -> dict:
    """What the recording actually touched, split into credentials and the rest.

    A recording is the only evidence the generator has about a page. When it
    contains nothing but a username and a password, the agents have no fields,
    no buttons and no grid to write about — and a model handed a URL like
    ``/masters/beamer-capacity`` will invent an entire module from those two
    words. That is what this exists to stop.
    """
    credential, business, buttons = [], [], []
    for act in actions or []:
        selector = (act.get("selector") or "").strip()
        if not selector or selector.startswith(("http://", "https://")):
            continue
        action = (act.get("action") or "").upper()
        blob = f"{selector} {act.get('label', '')} {act.get('text', '')}".lower()
        if any(marker in blob for marker in executor.CREDENTIAL_FIELD_MARKERS):
            credential.append(selector)
        elif action in ("TYPE", "SELECT", "CHECK", "UNCHECK", "UPLOAD"):
            business.append(selector)
        elif action in ("CLICK", "DOUBLE_CLICK"):
            buttons.append(selector)
    return {
        "credential_fields": sorted(set(credential)),
        "business_fields": sorted(set(business)),
        "clicked_controls": sorted(set(buttons)),
        # Login pages have a submit button, so a button alone does not make a
        # recording substantive.
        "credentials_only": bool(credential) and not business,
    }


def assert_recording_can_ground_generation(project_name: str, flow_name: str,
                                           actions: list, target_url: str) -> None:
    """Refuse to generate a suite the recording cannot possibly support.

    The failure this prevents, seen in real use: a flow named ``login`` was
    launched at ``/masters/beamer-capacity``, the app bounced to the login
    screen, and the recording captured only the credentials. Generation then
    produced 17 scenarios for a master-data module in which **every one of the
    27 selectors was invented** — the model had nothing but the URL to go on.
    Running them could only ever produce 17 failures that say nothing about the
    application.
    """
    grounding = recording_grounding(actions)
    if not grounding["credentials_only"]:
        return
    # A genuine login flow is credentials-only, and that is correct.
    if _is_login_url(target_url) or executor.is_auth_flow(flow_name):
        if _is_login_url(target_url):
            return
    learned_fields = learned.flow_profile(project_name, flow_name).get("fields") or []
    if learned_fields:
        return          # the engine has since inventoried the real page

    raise HTTPException(
        status_code=400,
        detail=(
            f"This recording only captured a login — the controls it touched are "
            f"{', '.join(grounding['credential_fields'] + grounding['clicked_controls'])}, "
            f"and no field belonging to the module — but the "
            f"flow's target URL is {target_url}. The agents would have to invent every "
            f"selector on that page, and the resulting suite would fail everywhere "
            f"without telling you anything about the application.\n\n"
            f"Re-record so the recording reaches the module itself: launch the browser, "
            f"log in, then navigate to the page, open its Add form and fill the fields. "
            f"Keep the login as its own separate flow — every other flow reuses the "
            f"stored session automatically."
        ),
    )


def generation_grounding(actions: list, cases: list) -> dict:
    """How much of a generated suite refers to something that was actually seen.

    A model must be allowed to name a Save button nobody recorded. It must not be
    allowed to invent a whole form. Reporting the ratio is what turns "17
    scenarios generated" into something a human can trust or reject.
    """
    recorded: set[str] = set()
    for act in actions or []:
        for candidate in [act.get("selector")] + list(act.get("selectors") or []):
            if candidate:
                recorded.add(str(candidate).strip())

    used: set[str] = set()
    for case in coverage.expand_scenarios(cases or []):
        for step in executor.parse_steps(case.get("steps") or []):
            selector = (step.get("selector") or "").strip()
            if selector and not selector.startswith(("http://", "https://")):
                used.add(selector)

    invented = sorted(s for s in used if s not in recorded)
    percent = round(len(invented) * 100 / len(used)) if used else 0
    return {
        "selectors_used": len(used),
        "selectors_recorded": len(recorded),
        "invented": invented[:20],
        "invented_count": len(invented),
        "invented_percent": percent,
        "grounded": percent <= 70,
    }


def describe_recorded_effects(actions: list) -> str:
    """The observed consequences of the recorded clicks, for the generation prompt.

    A recording that only lists clicks forces the agents to guess the expected
    result. When the recorder captured what followed — the exact message text,
    the URL change — the agents can assert reality instead.
    """
    lines: list[str] = []
    messages: list[str] = []
    for act in actions or []:
        effect = act.get("effect")
        if not isinstance(effect, dict):
            continue
        selector = act.get("selector") or "?"
        for message in (effect.get("messages") or [])[:2]:
            text = (message.get("text") or "").strip()
            if not text or text in messages:
                continue
            messages.append(text)
            lines.append(
                f'After clicking {selector} the application showed: "{text}" '
                f'(in {message.get("selector") or "unknown element"})'
            )
        before, after = effect.get("url_before", ""), effect.get("url_after", "")
        if before and after and before != after:
            lines.append(f"After clicking {selector} the URL changed to {after}")
    if not lines:
        return ""
    joined = "\n  ".join(lines[:12])
    return (
        "OBSERVED EFFECTS captured while recording — assert THESE, do not invent "
        f"wording:\n  {joined}\n"
        "  Use the exact text above in VERIFY_MESSAGE steps and in "
        "expected_result. If a message here is a success confirmation, the "
        "matching positive scenario MUST assert it."
    )


def build_fields_context(actions: list, project_name: str = "", flow_name: str = "") -> str:
    """Describe the fields a recording touched, marking the mandatory ones.

    Marking them matters: when an application keeps its Save button disabled
    until every required field is filled, a scenario that skips one can never
    click Save — the step fails for a reason that has nothing to do with what
    the scenario was trying to test.
    """
    fields: list[str] = []
    required: list[str] = []
    for act in actions:
        selector = act.get("selector", "")
        tag = act.get("tag", "")
        text = act.get("text", "") or act.get("label", "")
        action_type = (act.get("action") or "").upper()
        if not selector:
            continue
        if action_type in ("TYPE", "SELECT") or tag in ("input", "select", "textarea"):
            flag = " [REQUIRED]" if act.get("required") else ""
            entry = f"Field: {selector} (tag: {tag}, label: {text}){flag}"
            if entry not in fields:
                fields.append(entry)
            if act.get("required") and selector not in required:
                required.append(selector)
        elif action_type == "CLICK" and text:
            entry = f"Button/Link: {selector} (text: {text})"
            if entry not in fields:
                fields.append(entry)

    # Fields the engine discovered on the live page during earlier runs. A
    # recording only shows what the tester touched; this is everything the form
    # actually has, including the required field nobody recorded.
    if project_name and flow_name:
        try:
            profile = learned.flow_profile(project_name, flow_name)
            for observed in profile.get("fields", []):
                selector = observed.get("selector")
                if not selector:
                    continue
                # Record required-ness BEFORE the de-duplication. Skipping the
                # whole entry because the recording already described the field
                # threw away the one thing the recording could not know — that it
                # is mandatory. The result was a MANDATORY FIELDS line listing
                # only the fields the tester had *not* touched, which is exactly
                # backwards: Machine Name and Machine Type were dropped precisely
                # because they were recorded.
                if observed.get("required") and selector not in required:
                    required.append(selector)
                if any(selector in line for line in fields):
                    continue
                flag = " [REQUIRED]" if observed.get("required") else ""
                options = observed.get("options") or []
                choices = f" options: {options[:6]}" if options else ""
                fields.append(
                    f"Field (discovered on the page, not in the recording): {selector} "
                    f"(kind: {observed.get('kind', 'text')}, label: {observed.get('label', '')})"
                    f"{flag}{choices}"
                )
                if observed.get("required") and selector not in required:
                    required.append(selector)
            if profile.get("submit_gating") == "disabled":
                fields.append(
                    "OBSERVED BEHAVIOUR: this application keeps its submit button DISABLED "
                    "until the whole form is valid. It never shows a validation message for "
                    "an empty form, so a scenario that clicks Save and waits for one can "
                    "never pass. Assert VERIFY_DISABLED on the submit button instead."
                )
        except Exception:
            pass

    # What the recording observed *happening* — the effect of each click. This is
    # the difference between an agent inventing "a success message appears" and
    # writing VERIFY_MESSAGE with the wording the application actually showed.
    observed_effects = describe_recorded_effects(actions)
    if observed_effects:
        fields.append(observed_effects)

    if not fields:
        return "No specific fields detected from recording."
    if required:
        fields.append(
            "MANDATORY FIELDS: " + ", ".join(required) + ". Every scenario that expects a "
            "successful save must fill all of them, because this application keeps its "
            "submit button disabled until the form is valid. To test the empty-form case, "
            "assert the submit button is DISABLED (VERIFY_DISABLED) rather than clicking it "
            "and waiting for a validation message that will never appear."
        )
    return "\n".join(fields)


@app.post("/analyze-flow")
async def analyze_flow_api(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                           test_types: str = ""):
    flow_path = require_flow(project_name, flow_name)
    actions = read_json(flow_path / "actions.json", []) or []
    meta = read_json(flow_path / "flow.json", {}) or {}
    if not meta.get("target_url"):
        raise HTTPException(status_code=400, detail="This flow has no target URL recorded.")
    budget = llm.budget_status(planned_calls=1)
    if budget["exhausted"]:
        raise HTTPException(status_code=429,
                            detail=llm.quota_message(budget["model"], "daily"))

    # Before spending an API call: can this recording support a suite at all?
    assert_recording_can_ground_generation(
        project_name, flow_name, actions, meta["target_url"])

    if test_types.strip():
        types_guideline = (
            f"The user selected these test types: {test_types}. Generate a highly "
            "comprehensive set of test cases targeting these categories."
        )
    else:
        types_guideline = ("Generate a comprehensive, balanced mix of positive, negative "
                           "and boundary tests.")

    if executor.is_auth_flow(flow_name):
        instruction = f"""
            Generate between 10 and 12 comprehensive test cases for an AUTHENTICATION workflow.
            {types_guideline}
            Focus strictly on:
            - Valid credentials (positive login, remember-me checks).
            - Invalid credentials (wrong password, empty inputs, formatting validation).
            - Locked users, retry limits, session expiry triggers, redirection handling.
            Do NOT generate business transactional test cases.
        """
    else:
        instruction = f"""
            Generate between 12 and 15 comprehensive test cases for a BUSINESS TRANSACTION workflow.
            {types_guideline}
            Focus strictly on:
            - Full CRUD coverage (create, read/search, update, delete).
            - Field-by-Field Input Isolation: invalidate ONE field per scenario while the
              other fields stay valid.
            - Module functional logic (adding, deleting, filtering records, saving lists).
            - Boundary checks, invalid data types, business-rule and duplicate validations.
            CRITICAL RULES:
            - DO NOT generate login or authentication steps; the platform logs in beforehand.
            - Assume you are already logged in and start at the target module page.
        """

    prompt = ai_agents.build_spec_prompt(
        flow_name, meta["target_url"],
        llm.trim_for_prompt(json.dumps(actions, indent=2)), instruction,
    )

    try:
        analysis = await llm.generate_json(
            prompt,
            system_instruction=("You are an expert software QA planner. Generate a highly "
                                "structured, valid JSON test matrix. Output raw JSON only."),
        )
    except llm.LLMError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err)) from err

    if not isinstance(analysis, dict):
        raise HTTPException(status_code=502,
                            detail="The model did not return a specification object.")

    write_json(flow_path / "test_cases.json", analysis.get("test_cases", []))
    write_json(flow_path / "scripts.json", analysis.get("scripts", {
        "playwright_python": "", "cypress_js": "", "selenium_java": ""}))
    write_json(flow_path / "test_data.json", analysis.get("test_data", {
        "valid": [], "invalid": [], "boundary": []}))
    (flow_path / "documentation.md").write_text(
        analysis.get("documentation_markdown", ""), encoding="utf-8"
    )
    write_json(flow_path / "suggestions.json", analysis.get("suggestions", {
        "broken_selectors": [], "missing_validations": [], "recommendations": []}))
    write_json(flow_path / "spec.json", analysis)
    grounding = generation_grounding(actions, analysis.get("test_cases", []))
    db.audit("analyze_flow", f"{project_name}/{flow_name}",
             details={"cases": len(analysis.get("test_cases", [])),
                      "invented_percent": grounding["invented_percent"]})

    message = "AI analysis complete. Enterprise QA Specification generated successfully!"
    if not grounding["grounded"]:
        message = (
            f"Specification generated, but {grounding['invented_percent']}% of its "
            f"selectors ({grounding['invented_count']} of {grounding['selectors_used']}) "
            f"do not appear in the recording — the agents had to guess them. Run the "
            f"selector pre-flight before trusting this suite, or re-record the module "
            f"so the fields are captured."
        )
    return {"status": "success", "message": message, "grounding": grounding}


@app.get("/generate-multi-agent-stream")
async def generate_multi_agent_stream(flow_name: str,
                                      project_name: str = config.DEFAULT_PROJECT,
                                      test_types: str = "", max_iterations: int = 3):
    """Builder + Breaker in parallel, Auditor reviewing, looping to fill gaps."""
    flow_path = require_flow(project_name, flow_name)

    async def sse_generator():
        yield sse({"event": "start",
                   "message": "🚀 Multi-Agent Test Generation System initializing..."})

        if not config.GEMINI_API_KEY:
            yield sse({"event": "error",
                       "message": "GEMINI_API_KEY is not configured. Copy .env.example to "
                                  ".env, add your key and restart the server."})
            return

        actions = read_json(flow_path / "actions.json", []) or []
        meta = read_json(flow_path / "flow.json", {}) or {}
        target_url = meta.get("target_url", "")
        # Three agents across up to three rounds is up to nine requests. On a
        # small free tier that can exhaust the day mid-run and leave a partial
        # suite behind, so say so before starting rather than after failing.
        budget = llm.budget_status(planned_calls=3 * max(1, max_iterations))
        if budget["exhausted"]:
            yield sse({"event": "error",
                       "message": llm.quota_message(budget["model"], "daily")})
            return
        if not budget["enough"]:
            yield sse({"event": "log",
                       "message": f"⚠️ {budget['remaining']} request(s) likely remain today "
                                  f"for {budget['model']} and this run may need up to "
                                  f"{budget['planned_calls']}. It may stop part-way. "
                                  f"Reduce the rounds, or set QA_LLM_DAILY_BUDGET=0 if "
                                  f"billing is enabled."})

        # A recording that never reached the module cannot ground a suite for it.
        # Without this the agents invent every selector from the URL alone, which
        # burns API calls and produces scenarios that can only ever fail.
        try:
            assert_recording_can_ground_generation(
                project_name, flow_name, actions, target_url)
        except HTTPException as guard:
            yield sse({"event": "error", "message": guard.detail})
            return

        actions_json = llm.trim_for_prompt(json.dumps(actions, indent=2))
        fields_context = build_fields_context(actions, project_name, flow_name)

        # Your rules shape what gets generated, not just what gets checked.
        active_rules = rules.applicable(project_name, flow_name)
        rules_block = rules.as_prompt_block(active_rules)
        if active_rules:
            yield sse({"event": "log",
                       "message": f"📏 {len(active_rules)} of your rules are guiding generation."})

        system_instruction = ("You are an expert software QA engineer. Output ONLY valid JSON. "
                              "No markdown, no explanations, no commentary.")
        all_cases: list[dict] = []
        last_missing: list = []
        iteration = 0
        coverage_complete = False
        coverage_pct = 0

        while iteration < max(1, max_iterations) and not coverage_complete:
            iteration += 1
            yield sse({"event": "round_start", "round": iteration,
                       "max_rounds": max_iterations,
                       "message": f"━━━ Round {iteration}/{max_iterations} ━━━"})

            missing_1 = missing_2 = None
            if iteration > 1 and last_missing:
                functional = [m for m in last_missing if m.get("suggested_type") in
                              ("positive", "positive_ui", "positive_boundary", "functional")]
                edge = [m for m in last_missing if m.get("suggested_type") in
                        ("negative", "boundary", "security", "negative_boundary")]
                untyped = [m for m in last_missing if m.get("suggested_type") not in
                           ("positive", "positive_ui", "positive_boundary", "functional",
                            "negative", "boundary", "security", "negative_boundary")]
                missing_1 = (functional + untyped) or None
                missing_2 = (edge + untyped) or None

            yield sse({"event": "agent_start", "agent": 1,
                       "name": "Functional Generator (The Builder)",
                       "message": "🤖 Agent 1 & Agent 2 generating test cases in parallel..."})

            prompt1 = ai_agents._build_agent1_prompt(
                flow_name, target_url, actions_json, fields_context, missing_1, test_types,
                rules_block)
            prompt2 = ai_agents._build_agent2_prompt(
                flow_name, target_url, actions_json, fields_context, missing_2, test_types,
                rules_block)

            results = await asyncio.gather(
                llm.generate_json(prompt1, system_instruction=system_instruction),
                llm.generate_json(prompt2, system_instruction=system_instruction),
                return_exceptions=True,
            )

            agent_cases: list[list[dict]] = []
            # Kept so the closing message can report the real cause instead of
            # generic advice pointing at things the errors above already ruled out.
            agent_errors: list[str] = []
            for agent_no, result in enumerate(results, start=1):
                if isinstance(result, Exception):
                    agent_errors.append(str(result))
                    yield sse({"event": "agent_error", "agent": agent_no,
                               "message": f"⚠️ Agent {agent_no} error: {result}"})
                    agent_cases.append([])
                else:
                    agent_cases.append(ai_agents.normalise_generated_cases(result))

            for agent_no, cases in enumerate(agent_cases, start=1):
                existing = {c.get("scenario_name", "").strip().lower() for c in all_cases}
                added = 0
                for case in cases:
                    name = (case.get("scenario_name") or "").strip().lower()
                    if name and name not in existing:
                        all_cases.append(case)
                        existing.add(name)
                        added += 1
                label = "functional" if agent_no == 1 else "edge case"
                # A tick against "generated 0 test cases" reads as success and
                # buries the error line directly above it.
                mark = "✅" if cases else "❌"
                yield sse({"event": "agent_done", "agent": agent_no, "count": len(cases),
                           "unique_added": added, "ok": bool(cases),
                           "message": f"{mark} Agent {agent_no} generated {len(cases)} {label} "
                                      f"test cases ({added} unique)."})

            yield sse({"event": "merge", "total": len(all_cases),
                       "message": f"📋 Merged suite: {len(all_cases)} total unique test cases."})

            if not all_cases:
                if agent_errors:
                    detail = " ".join(dict.fromkeys(agent_errors))
                    message = f"No test cases were produced — every agent failed. {detail}"
                else:
                    # No exception and still nothing: the model answered with
                    # something that held no test cases. "Check the API key"
                    # here sent the reader at the one thing that provably
                    # worked — the call had succeeded.
                    message = (f"No test cases were produced. {config.GEMINI_MODEL} "
                               f"replied, but the response held no usable test cases. "
                               f"Retry once; if it repeats, the raw reply is in "
                               f"logs/qa_platform.log.")
                yield sse({"event": "error", "message": message})
                return

            yield sse({"event": "agent_start", "agent": 3, "name": "QA Reviewer (The Auditor)",
                       "message": "🔍 Agent 3 reviewing coverage and identifying gaps..."})
            try:
                review = await llm.generate_json(
                    ai_agents._build_agent3_prompt(all_cases, flow_name, fields_context,
                                                   rules_block),
                    system_instruction=system_instruction,
                )
                if isinstance(review, list):
                    review = review[0] if review else {}
                coverage_pct = review.get("coverage_percentage", 0)
                covered = review.get("covered_items", []) or []
                missing = review.get("missing_items", []) or []
                duplicates = review.get("duplicate_scenarios", []) or []
                coverage_complete = bool(review.get("coverage_complete", False))
                last_missing = missing

                yield sse({"event": "review_done", "agent": 3,
                           "coverage_percentage": coverage_pct,
                           "covered_count": len(covered), "missing_count": len(missing),
                           "coverage_complete": coverage_complete, "missing_items": missing,
                           "review_notes": review.get("review_notes", ""),
                           "message": f"🔍 Coverage: {coverage_pct}% | Covered: {len(covered)} "
                                      f"| Gaps: {len(missing)}"})
                if duplicates:
                    yield sse({"event": "log",
                               "message": f"🔄 {len(duplicates)} duplicate scenarios detected."})
                if coverage_complete:
                    yield sse({"event": "log",
                               "message": "🎉 Full coverage achieved! No more iterations needed."})
                elif iteration >= max_iterations:
                    yield sse({"event": "log",
                               "message": f"⏱️ Max iterations ({max_iterations}) reached. "
                                          f"Finalizing with current coverage."})
                else:
                    yield sse({"event": "log",
                               "message": f"🔁 {len(missing)} gaps found. Starting Round "
                                          f"{iteration + 1} to fill them..."})
            except Exception as err:
                yield sse({"event": "agent_error", "agent": 3,
                           "message": f"⚠️ Agent 3 review error: {err}. Accepting current suite."})
                coverage_complete = True

        # Stamp assertion coverage so the UI can warn about unprovable scenarios.
        without_assertions = [
            c.get("scenario_name") for c in all_cases
            if not any(
                (s.get("action", "") if isinstance(s, dict) else str(s)).upper().startswith(
                    ("VERIFY", "ASSERT"))
                for s in c.get("steps", [])
            )
        ]
        grounding = generation_grounding(actions, all_cases)
        if not grounding["grounded"]:
            yield sse({"event": "log",
                       "message": f"⚠️ {grounding['invented_percent']}% of the generated "
                                  f"selectors ({grounding['invented_count']} of "
                                  f"{grounding['selectors_used']}) are not in the recording "
                                  f"— the agents guessed them. Run the selector pre-flight "
                                  f"before trusting this suite."})

        write_json(flow_path / "test_cases.json", all_cases)
        write_json(flow_path / "generation_meta.json", {
            "grounding": grounding,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rounds": iteration,
            "coverage_percentage": coverage_pct,
            "total_cases": len(all_cases),
            "scenarios_without_assertions": without_assertions,
        })
        db.audit("multi_agent_generate", f"{project_name}/{flow_name}",
                 details={"cases": len(all_cases), "rounds": iteration})

        yield sse({"event": "saved", "total_cases": len(all_cases),
                   "message": f"💾 Final suite saved: {len(all_cases)} test cases → "
                              f"test_cases.json"})
        if without_assertions:
            yield sse({"event": "log",
                       "message": f"⚠️ {len(without_assertions)} scenario(s) have no VERIFY step "
                                  f"and can only ever return UNVERIFIED. Consider re-running "
                                  f"generation or adding assertions in the editor."})
        yield sse({"event": "complete", "total_cases": len(all_cases),
                   "rounds_executed": iteration,
                   "message": f"✅ Multi-Agent generation complete! {len(all_cases)} test cases "
                              f"generated across {iteration} round(s)."})

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


# ──────────────────────────────────────────────────────────────────────
# Selector pre-flight validation
# ──────────────────────────────────────────────────────────────────────
DOM_SUMMARY_JS = """() => {
    const out = [];
    document.querySelectorAll(
        'input, select, textarea, button, a[href], [role="button"], [role="combobox"]'
    ).forEach(el => {
        if (out.length > 220) return;
        const tag = el.tagName.toLowerCase();
        out.push({
            tag,
            id: el.id || '',
            name: el.getAttribute('name') || '',
            formcontrolname: el.getAttribute('formcontrolname') || '',
            placeholder: el.getAttribute('placeholder') || '',
            aria: el.getAttribute('aria-label') || '',
            testid: el.getAttribute('data-testid') || '',
            type: el.getAttribute('type') || '',
            text: (el.innerText || '').trim().substring(0, 40)
        });
    });
    return out;
}"""


@app.get("/preflight-selectors-stream")
async def preflight_selectors_stream(flow_name: str,
                                     project_name: str = config.DEFAULT_PROJECT,
                                     apply_fix: bool = False, mode: str = "headless"):
    """Load the page and check that every generated selector really exists."""
    flow_path = require_flow(project_name, flow_name)

    async def sse_generator():
        cases = read_json(flow_path / "test_cases.json", []) or []
        if not cases:
            yield sse({"event": "error", "message": "No test cases to validate."})
            return

        meta = read_json(flow_path / "flow.json", {}) or {}
        target_url = executor.resolve_target_url(project_name, meta)
        if not target_url:
            yield sse({"event": "error", "message": "This flow has no target URL."})
            return

        selectors: dict[str, list[str]] = {}
        for case in cases:
            for step in executor.parse_steps(case.get("steps", [])):
                sel = (step.get("selector") or "").strip()
                action = (step.get("action") or "").upper()
                if not sel or action in ("NAVIGATE", "WAIT", "DIALOG", "KEYPRESS"):
                    continue
                if sel.startswith(("http://", "https://")):
                    continue
                selectors.setdefault(sel, []).append(case.get("scenario_name", ""))

        yield sse({"event": "start",
                   "message": f"🔍 Pre-flight: checking {len(selectors)} unique selectors "
                              f"from {len(cases)} scenarios against the live page..."})

        found: list[str] = []
        missing: list[dict] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=(mode != "headed"))
            context_args: dict[str, Any] = {}
            session_file = config.project_session_file(project_name)
            if session_file.exists():
                context_args["storage_state"] = str(session_file)
            context = await browser.new_context(**context_args)
            page = await context.new_page()
            page.set_default_timeout(8000)
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                await executor.wait_for_settle(page)
                if "auth/login" in page.url:
                    yield sse({"event": "log",
                               "message": "⚠️ Redirected to the login page — validating the "
                                          "login screen only. Run a suite once to store a "
                                          "session, then re-check."})

                dom_summary = await page.evaluate(DOM_SUMMARY_JS)

                for selector, used_by in selectors.items():
                    exists = False
                    try:
                        exists = await page.locator(selector).count() > 0
                    except Exception:
                        exists = False
                    if not exists:
                        for candidate in executor.candidate_selectors(
                            {"selector": selector}, "CLICK"
                        )[1:]:
                            try:
                                if await page.locator(candidate).count() > 0:
                                    exists = True
                                    break
                            except Exception:
                                continue
                    if exists:
                        found.append(selector)
                    else:
                        missing.append({"selector": selector,
                                        "used_by": sorted(set(used_by))[:5]})
                    yield sse({"event": "progress", "checked": len(found) + len(missing),
                               "total": len(selectors), "selector": selector,
                               "exists": exists})

                yield sse({"event": "summary", "total": len(selectors), "found": len(found),
                           "missing": len(missing), "missing_items": missing,
                           "message": f"✅ {len(found)} selectors resolved, "
                                      f"❌ {len(missing)} missing."})

                if missing and apply_fix and config.GEMINI_API_KEY:
                    yield sse({"event": "log",
                               "message": "🛠️ Asking the model to map broken selectors onto "
                                          "real elements..."})
                    try:
                        repair = await llm.generate_json(
                            ai_agents.build_selector_repair_prompt(
                                flow_name, [m["selector"] for m in missing],
                                json.dumps(dom_summary, indent=1)[:12000],
                            ),
                            system_instruction="Output only valid JSON.",
                        )
                        repairs = [
                            r for r in (repair.get("repairs") or [])
                            if r.get("replacement") and float(r.get("confidence", 0)) >= 0.7
                        ]
                        applied = 0
                        for item in repairs:
                            broken, replacement = item["broken"], item["replacement"]
                            try:
                                if await page.locator(replacement).count() == 0:
                                    continue
                            except Exception:
                                continue
                            for case in cases:
                                for step in case.get("steps", []):
                                    if isinstance(step, dict) and step.get("selector") == broken:
                                        step["selector"] = replacement
                                        applied += 1
                            yield sse({"event": "repair", "broken": broken,
                                       "replacement": replacement,
                                       "message": f"🛠️ {broken} → {replacement} "
                                                  f"({item.get('reason', '')})"})
                        if applied:
                            write_json(flow_path / "test_cases.json", cases)
                            yield sse({"event": "log",
                                       "message": f"💾 Applied {applied} selector repair(s) to "
                                                  f"test_cases.json."})
                    except Exception as err:
                        yield sse({"event": "log", "message": f"⚠️ Repair pass failed: {err}"})
            except Exception as err:
                yield sse({"event": "error",
                           "message": executor.humanize_error(err)})
            finally:
                try:
                    await context.close()
                    await browser.close()
                except Exception:
                    pass
        yield sse({"event": "complete", "message": "Pre-flight validation finished."})

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


# ──────────────────────────────────────────────────────────────────────
# Test-case management
# ──────────────────────────────────────────────────────────────────────
@app.post("/upload-test-cases")
async def upload_test_cases(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                            file: UploadFile = File(...)):
    flow_path = require_flow(project_name, flow_name)
    try:
        contents = await file.read()
        test_cases = json.loads(contents.decode("utf-8"))
    except json.JSONDecodeError as err:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file format: {err}") from err
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err)) from err

    if not isinstance(test_cases, list):
        raise HTTPException(status_code=400, detail="JSON must contain a list of test cases.")

    write_json(flow_path / "test_cases.json", test_cases)
    return {"status": "success", "message": "Test cases uploaded and saved successfully."}


@app.post("/update-test-case")
async def update_test_case(flow_name: str, case_index: int,
                           project_name: str = config.DEFAULT_PROJECT,
                           payload: dict = Body(...)):
    """Replace one scenario (1-based index) with an edited version."""
    flow_path = require_flow(project_name, flow_name)
    cases = read_json(flow_path / "test_cases.json", []) or []
    if not 1 <= case_index <= len(cases):
        raise HTTPException(status_code=404, detail="Test case index out of range.")

    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else payload
    for required in ("scenario_name", "steps"):
        if required not in scenario:
            raise HTTPException(status_code=400,
                                detail=f"The scenario must include '{required}'.")
    if not isinstance(scenario["steps"], list):
        raise HTTPException(status_code=400, detail="'steps' must be a list.")

    scenario.pop("_original_id", None)
    cases[case_index - 1] = scenario
    write_json(flow_path / "test_cases.json", cases)
    db.audit("update_test_case", f"{project_name}/{flow_name}", details={"index": case_index})
    return {"status": "success", "message": f"Scenario #{case_index} updated."}


@app.post("/add-test-case")
async def add_test_case(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                        payload: dict | None = Body(default=None)):
    """Append a scenario. With no body, adds an empty template to edit."""
    flow_path = require_flow(project_name, flow_name)
    cases = read_json(flow_path / "test_cases.json", []) or []
    payload = payload or {}
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else payload
    scenario.setdefault("scenario_name", f"New scenario {len(cases) + 1}")
    scenario.setdefault("type", "positive")
    scenario.setdefault("steps", [])
    scenario.setdefault("expected_result", "")
    cases.append(scenario)
    write_json(flow_path / "test_cases.json", cases)
    return {"status": "success", "message": "Scenario added.", "index": len(cases)}


@app.post("/delete-test-case")
async def delete_test_case(flow_name: str, case_index: int,
                           project_name: str = config.DEFAULT_PROJECT):
    flow_path = require_flow(project_name, flow_name)
    cases = read_json(flow_path / "test_cases.json", []) or []
    if not 1 <= case_index <= len(cases):
        raise HTTPException(status_code=404, detail="Test case index out of range.")
    removed = cases.pop(case_index - 1)
    write_json(flow_path / "test_cases.json", cases)
    db.audit("delete_test_case", f"{project_name}/{flow_name}",
             details={"name": removed.get("scenario_name")})
    return {"status": "success",
            "message": f"Removed '{removed.get('scenario_name', 'scenario')}'."}


@app.post("/upload-data-matrix")
async def upload_data_matrix(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                             file: UploadFile = File(...)):
    """Upload a CSV whose columns become {{variables}} for data-driven runs."""
    flow_path = require_flow(project_name, flow_name)
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file.")
    data_dir = flow_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    contents = await file.read()
    dest = data_dir / Path(file.filename).name
    dest.write_bytes(contents)
    rows = executor.load_data_matrix(flow_path, dest.name)
    return {"status": "success", "filename": dest.name, "rows": len(rows),
            "columns": sorted(rows[0].keys()) if rows else [],
            "message": f"Loaded {len(rows)} data row(s) from {dest.name}."}


# ──────────────────────────────────────────────────────────────────────
# Execution
# ──────────────────────────────────────────────────────────────────────
def build_run_config(**kwargs) -> executor.RunConfig:
    cfg = executor.RunConfig(**kwargs)
    if cfg.verdict_mode not in ("off", "rules", "ai"):
        cfg.verdict_mode = "rules"
    if cfg.step_shots not in ("off", "fail", "all"):
        cfg.step_shots = "fail"
    return cfg


@app.get("/run-matrix-stream")
async def run_matrix_stream(flow_name: str, num_cases: int = -1, mode: str = "headless",
                            project_name: str = config.DEFAULT_PROJECT, test_types: str = "",
                            auth_flow: str = "", auth_scenario: str = "",
                            dependencies: str = "", case_ids: str = "", workers: int = 1,
                            verdict_mode: str = "rules", step_shots: str = "fail",
                            tracing: bool = False, dep_unique_data: bool = False,
                            data_matrix: str = "", visual_check: bool = False,
                            env_name: str = "", api_seed_name: str = "",
                            seed_once: bool = True, relationship_checks: bool = False,
                            apply_rules: bool = True):
    """Execute the suite, streaming progress over SSE (dashboard entry point)."""
    validate_safe_path(project_name, flow_name, auth_flow)
    flow_path = config.flow_dir(project_name, flow_name)
    if not (flow_path / "test_cases.json").exists():
        raise HTTPException(status_code=400, detail="No test cases generated for this flow.")

    cfg = build_run_config(
        project_name=project_name, flow_name=flow_name, num_cases=num_cases, mode=mode,
        test_types=test_types, auth_flow=auth_flow, auth_scenario=auth_scenario,
        dependencies=dependencies, case_ids=case_ids, workers=workers,
        verdict_mode=verdict_mode, step_shots=step_shots, tracing=tracing,
        dep_unique_data=dep_unique_data, data_matrix=data_matrix,
        visual_check=visual_check, env_name=env_name, api_seed=api_seed_name,
        seed_once=seed_once, relationship_checks=relationship_checks,
        apply_rules=apply_rules, triggered_by="dashboard",
    )
    state = executor.register_run(project_name, flow_name)
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def worker() -> None:
        try:
            await executor.execute_suite(cfg, emit, state)
        except Exception as err:  # noqa: BLE001
            log.exception("Suite execution failed")
            state.status = "error"
            state.error = str(err)
            await queue.put({"event": "error",
                             "message": executor.humanize_error(err)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(worker())

    async def sse_generator():
        yield sse({"event": "log", "run_id": state.run_id,
                   "message": f"🆔 Run {state.run_id} registered "
                              f"(use Stop to cancel it at any time)."})
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield sse(event)
        finally:
            # Client closed the tab / pressed stop → don't leave a browser running.
            if not task.done():
                state.cancelled = True

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.post("/stop-run")
async def stop_run(run_id: str = "", project_name: str = "", flow_name: str = ""):
    if run_id:
        if executor.cancel_run(run_id):
            return {"status": "success", "message": f"Run {run_id} is stopping..."}
        raise HTTPException(status_code=404, detail="That run is not active.")

    stopped = 0
    for state in list(executor.RUN_REGISTRY.values()):
        if state.status != "running":
            continue
        if project_name and state.project_name != project_name:
            continue
        if flow_name and state.flow_name != flow_name:
            continue
        stopped += 1 if executor.cancel_run(state.run_id) else 0
    if not stopped:
        raise HTTPException(status_code=404, detail="No active runs to stop.")
    return {"status": "success", "message": f"Stopping {stopped} active run(s)..."}


@app.post("/trigger-run")
async def trigger_run(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                      mode: str = "headless", test_types: str = "", dependencies: str = "",
                      auth_flow: str = "", auth_scenario: str = "", workers: int = 2,
                      verdict_mode: str = "rules", env_name: str = "", num_cases: int = -1,
                      api_seed_name: str = "", case_ids: str = "", step_shots: str = "fail",
                      tracing: bool = False, dep_unique_data: bool = False,
                      data_matrix: str = "", visual_check: bool = False,
                      seed_once: bool = True, relationship_checks: bool = False,
                      apply_rules: bool = True, triggered_by: str = "ci"):
    """Fire-and-forget entry point for CI/CD. Returns a run_id to poll.

    Accepts the same knobs as the dashboard stream so a pipeline can run a
    subset (``case_ids``), a data matrix, or with tracing enabled.
    """
    validate_safe_path(project_name, flow_name)
    flow_path = config.flow_dir(project_name, flow_name)
    if not (flow_path / "test_cases.json").exists():
        raise HTTPException(status_code=400, detail="No test cases generated for this flow.")

    cfg = build_run_config(
        project_name=project_name, flow_name=flow_name, num_cases=num_cases, mode=mode,
        test_types=test_types, dependencies=dependencies, auth_flow=auth_flow,
        auth_scenario=auth_scenario, workers=workers, verdict_mode=verdict_mode,
        env_name=env_name, api_seed=api_seed_name, case_ids=case_ids,
        step_shots=step_shots, tracing=tracing, dep_unique_data=dep_unique_data,
        data_matrix=data_matrix, visual_check=visual_check, seed_once=seed_once,
        relationship_checks=relationship_checks, apply_rules=apply_rules,
        triggered_by=triggered_by,
    )
    state = executor.register_run(project_name, flow_name)

    async def emit(event: dict) -> None:
        if event.get("event") in ("test_end", "complete", "error", "cancelled"):
            state.events.append(event)

    async def background() -> None:
        try:
            report = await executor.execute_suite(cfg, emit, state)
            await scheduler.notify(project_name, report)
        except Exception as err:  # noqa: BLE001
            log.exception("Triggered run failed")
            state.status = "error"
            state.error = str(err)

    asyncio.create_task(background())
    return {
        "status": "accepted",
        "run_id": state.run_id,
        "poll": f"/run-status/{state.run_id}",
        "junit": f"/export-junit?project_name={project_name}&flow_name={flow_name}",
    }


@app.get("/run-status/{run_id}")
async def run_status(run_id: str, response: Response, include_report: bool = False):
    no_cache(response)
    state = executor.RUN_REGISTRY.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown run id.")
    payload = state.snapshot()
    payload["exit_code"] = 0 if (state.status == "complete" and not state.failed
                                 and not state.crashed) else 1
    if include_report and state.report:
        payload["report"] = state.report
    return payload


@app.get("/list-runs")
async def list_runs(response: Response, project_name: str = "", flow_name: str = "",
                    limit: int = 50):
    no_cache(response)
    live = [
        state.snapshot() for state in executor.RUN_REGISTRY.values()
        if (not project_name or state.project_name == project_name)
        and (not flow_name or state.flow_name == flow_name)
    ]
    live.sort(key=lambda item: item["started_at"], reverse=True)
    return {"active": [r for r in live if r["status"] == "running"],
            "recent": live[:limit],
            "history": db.recent_runs(project_name, flow_name, limit)}


# ──────────────────────────────────────────────────────────────────────
# Reports, history, exports
# ──────────────────────────────────────────────────────────────────────
@app.get("/get-run-history")
async def get_run_history(flow_name: str, response: Response,
                          project_name: str = config.DEFAULT_PROJECT):
    flow_path = require_flow(project_name, flow_name)
    no_cache(response)
    history = read_json(flow_path / "run_history.json", []) or []
    return {"history": history[::-1]}


@app.get("/get-report")
async def get_report(flow_name: str, response: Response,
                     project_name: str = config.DEFAULT_PROJECT, report_file: str = ""):
    flow_path = require_flow(project_name, flow_name)
    no_cache(response)
    if report_file:
        if ".." in report_file or "\\" in report_file:
            raise HTTPException(status_code=400, detail="Invalid report reference.")
        path = flow_path / report_file
    else:
        path = flow_path / "report.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    return read_json(path, {})


@app.get("/compare-runs")
async def compare_runs(flow_name: str, response: Response,
                       project_name: str = config.DEFAULT_PROJECT,
                       report_a: str = "", report_b: str = ""):
    """Diff two runs: which scenarios flipped, plus timing changes."""
    flow_path = require_flow(project_name, flow_name)
    no_cache(response)
    history = read_json(flow_path / "run_history.json", []) or []
    if not report_a or not report_b:
        if len(history) < 2:
            raise HTTPException(status_code=400,
                                detail="At least two runs are needed to compare.")
        report_b = report_b or history[-1]["report_file"]
        report_a = report_a or history[-2]["report_file"]

    def load(rel: str) -> dict:
        if ".." in rel or "\\" in rel:
            raise HTTPException(status_code=400, detail="Invalid report reference.")
        data = read_json(flow_path / rel, None)
        if data is None:
            raise HTTPException(status_code=404, detail=f"Report {rel} not found.")
        return data

    first, second = load(report_a), load(report_b)
    by_name_a = {c.get("name"): c for c in first.get("checkpoints", [])}
    by_name_b = {c.get("name"): c for c in second.get("checkpoints", [])}

    flipped, fixed, broken, unchanged, new_cases, removed = [], [], [], [], [], []
    for name, case_b in by_name_b.items():
        case_a = by_name_a.get(name)
        if not case_a:
            new_cases.append({"name": name, "status": case_b.get("status")})
            continue
        status_a, status_b = case_a.get("status"), case_b.get("status")
        entry = {
            "name": name, "from": status_a, "to": status_b,
            "reason": (case_b.get("verdict") or {}).get("reason") or case_b.get("error", ""),
            "duration_delta": (case_b.get("duration", 0) - case_a.get("duration", 0)),
        }
        if status_a == status_b:
            unchanged.append(entry)
            continue
        flipped.append(entry)
        if status_b == "PASS":
            fixed.append(entry)
        elif status_a == "PASS":
            broken.append(entry)
    for name, case_a in by_name_a.items():
        if name not in by_name_b:
            removed.append({"name": name, "status": case_a.get("status")})

    return {
        "report_a": {"file": report_a, "date": first.get("execution_date"),
                     "passed": first.get("passed_count"), "failed": first.get("failed_count"),
                     "unverified": first.get("unverified_count", 0)},
        "report_b": {"file": report_b, "date": second.get("execution_date"),
                     "passed": second.get("passed_count"), "failed": second.get("failed_count"),
                     "unverified": second.get("unverified_count", 0)},
        "flipped": flipped, "newly_failing": broken, "newly_passing": fixed,
        "unchanged_count": len(unchanged), "new_scenarios": new_cases,
        "removed_scenarios": removed,
    }


@app.get("/get-project-bugs")
async def get_project_bugs(project_name: str, response: Response):
    validate_safe_path(project_name)
    no_cache(response)
    bugs = read_json(config.project_dir(project_name) / "bugs.json", []) or []
    return {"bugs": bugs}


@app.get("/scenario-history")
async def scenario_history(project_name: str, flow_name: str, scenario_name: str,
                           response: Response):
    validate_safe_path(project_name, flow_name)
    no_cache(response)
    return {"history": db.scenario_history(project_name, flow_name, scenario_name)}


@app.get("/flaky-tests")
async def flaky_tests(response: Response, project_name: str = "", window: int = 5):
    no_cache(response)
    return {"flaky": db.flaky_scenarios(project_name, window=window)}


# ══════════════════════════════════════════════════════════════════════
# Rules — your knowledge of the application, enforced and generated from
# ══════════════════════════════════════════════════════════════════════
@app.get("/get-rules")
async def get_rules(response: Response, project_name: str = config.DEFAULT_PROJECT,
                    flow_name: str = ""):
    no_cache(response)
    validate_safe_path(project_name, flow_name)
    return {
        "project_rules": rules._read(rules.project_path(project_name)),
        "flow_rules": rules._read(rules.flow_path(project_name, flow_name)) if flow_name else [],
        "applicable": rules.applicable(project_name, flow_name) if flow_name else [],
        "kinds": list(rules.KINDS),
        "severities": list(rules.SEVERITIES),
    }


@app.post("/save-rule")
async def save_rule(project_name: str = config.DEFAULT_PROJECT, flow_name: str = "",
                    payload: dict = Body(...)):
    validate_safe_path(project_name, flow_name)
    try:
        saved = rules.save(project_name, payload, flow_name)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    db.audit("save_rule", f"{project_name}/{flow_name or '*'}", details={"id": saved["id"]})
    return {"status": "success", "rule": saved}


@app.post("/save-rules")
async def save_rules_bulk(project_name: str = config.DEFAULT_PROJECT, flow_name: str = "",
                          payload: dict = Body(...)):
    """Save a reviewed batch — used after translating plain English."""
    validate_safe_path(project_name, flow_name)
    items = payload.get("rules") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Expected a 'rules' array.")
    try:
        saved = rules.save_many(project_name, items, flow_name)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"status": "success", "saved": len(saved), "rules": saved}


@app.post("/delete-rule")
async def delete_rule(rule_id: str, project_name: str = config.DEFAULT_PROJECT,
                      flow_name: str = ""):
    validate_safe_path(project_name, flow_name)
    if not rules.delete(project_name, rule_id, flow_name):
        raise HTTPException(status_code=404, detail="Rule not found.")
    return {"status": "success", "message": f"Rule {rule_id} deleted."}


@app.post("/translate-rules")
async def translate_rules(project_name: str = config.DEFAULT_PROJECT, flow_name: str = "",
                          payload: dict = Body(...)):
    """Plain English in, structured rules out — for review, never auto-saved."""
    validate_safe_path(project_name, flow_name)
    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write at least one rule first.")
    if not config.GEMINI_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="GEMINI_API_KEY is not configured, so plain-English rules cannot be "
                   "translated. You can still add rules manually.",
        )
    fields_context = ""
    if flow_name:
        flow_path = config.flow_dir(project_name, flow_name)
        actions = read_json(flow_path / "actions.json", []) or []
        fields_context = build_fields_context(actions, project_name, flow_name)
    try:
        parsed = await llm.generate_json(
            rules.build_translate_prompt(text, fields_context),
            system_instruction="You output only a JSON array of rule objects.",
        )
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"Translation failed: {err}")
    translated = rules.coerce_translated(parsed)
    if not translated:
        raise HTTPException(
            status_code=422,
            detail="Nothing usable came back. Try phrasing one rule per line, naming the "
                   "field and the exact message or URL involved.",
        )
    return {"status": "success", "rules": translated,
            "note": "Review these, then save. Nothing has been stored yet."}


# ══════════════════════════════════════════════════════════════════════
# Dependency contracts — what a flow provides and requires
# ══════════════════════════════════════════════════════════════════════
@app.get("/get-contract")
async def get_contract(response: Response, flow_name: str,
                       project_name: str = config.DEFAULT_PROJECT, dependencies: str = ""):
    no_cache(response)
    require_flow(project_name, flow_name)
    contract = contracts.load(project_name, flow_name)
    return {
        "contract": contract,
        "providers": contracts.provider_map(project_name, dependencies),
        "unresolved": contracts.unresolved_requirements(project_name, flow_name, dependencies),
        "kinds": list(contracts.KINDS),
    }


@app.post("/save-contract")
async def save_contract(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                        payload: dict = Body(...)):
    require_flow(project_name, flow_name)
    try:
        saved = contracts.save(project_name, flow_name, payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"status": "success", "contract": saved}


@app.get("/preview-relationship-checks")
async def preview_relationship_checks(response: Response, flow_name: str,
                                      project_name: str = config.DEFAULT_PROJECT,
                                      dependencies: str = ""):
    """What the engine would add for this chain, without running anything."""
    no_cache(response)
    require_flow(project_name, flow_name)
    scenarios, notes = contracts.relationship_checks(project_name, flow_name, dependencies)
    return {"scenarios": scenarios, "notes": notes, "count": len(scenarios)}


# ──────────────────────────────────────────────────────────────────────
# Coverage — scenarios, their test cases, and what is missing
# ──────────────────────────────────────────────────────────────────────
def _form_fields(project_name: str, flow_name: str) -> list[dict]:
    """Fields for this module: what the engine discovered, then the recording.

    The learned profile is preferred because it is the whole form, including the
    mandatory field nobody recorded. The recording is the fallback for a flow
    that has never run.
    """
    fields = list(learned.flow_profile(project_name, flow_name).get("fields") or [])
    if fields:
        return fields
    flow_path = config.flow_dir(project_name, flow_name)
    seen: set[str] = set()
    for act in read_json(flow_path / "actions.json", []) or []:
        selector = (act.get("selector") or "").strip()
        action = (act.get("action") or "").upper()
        tag = act.get("tag", "")
        if not selector or selector in seen:
            continue
        if action in ("TYPE", "SELECT") or tag in ("input", "select", "textarea"):
            seen.add(selector)
            fields.append({"selector": selector, "label": act.get("label") or act.get("text", ""),
                           "kind": "text", "required": bool(act.get("required"))})
    return fields


CONTROL_INVENTORY_JS = """() => {
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = getComputedStyle(el);
        return style.visibility !== 'hidden' && style.display !== 'none';
    };
    const label = (el) => {
        const own = (el.innerText || el.value || '').trim();
        if (own) return own.substring(0, 60);
        for (const attr of ['aria-label', 'title', 'placeholder', 'name']) {
            const v = el.getAttribute(attr);
            if (v) return v.substring(0, 60);
        }
        if (el.id) {
            const lab = document.querySelector(`label[for="${el.id}"]`);
            if (lab && lab.innerText.trim()) return lab.innerText.trim().substring(0, 60);
        }
        return '';
    };
    const stable = (el) => {
        for (const attr of ['data-testid', 'data-test', 'data-cy']) {
            const v = el.getAttribute(attr);
            if (v) return `[${attr}="${v}"]`;
        }
        if (el.id && !/^(pn_id_|p-|mat-|cdk-|ng-)/i.test(el.id)) return '#' + el.id;
        const fcn = el.getAttribute('formcontrolname');
        if (fcn) return `${el.tagName.toLowerCase()}[formcontrolname="${fcn}"]`;
        if (el.getAttribute('name')) {
            return `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"]`;
        }
        const text = (el.innerText || '').trim();
        if (text && text.length < 40) {
            return `${el.tagName.toLowerCase()}:has-text("${text.replace(/"/g, '')}")`;
        }
        const ph = el.getAttribute('placeholder');
        if (ph) return `${el.tagName.toLowerCase()}[placeholder="${ph}"]`;
        return '';
    };

    const out = [];
    const seen = new Set();
    document.querySelectorAll(
        'input, select, textarea, button, a[href], [role="button"], [role="link"], ' +
        '[role="tab"], [role="checkbox"], [role="switch"]'
    ).forEach((el) => {
        if (out.length > 300) return;
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'hidden') return;
        if (!visible(el)) return;
        const selector = stable(el);
        if (!selector || seen.has(selector)) return;
        seen.add(selector);
        const href = el.getAttribute('href') || '';
        out.push({
            selector,
            tag: el.tagName.toLowerCase(),
            type,
            label: label(el),
            href: href.startsWith('javascript:') ? '' : href,
        });
    });
    return out;
}"""

#: Controls whose absence from a suite is never worth reporting.
IGNORABLE_CONTROL_WORDS = (
    "cookie", "privacy", "terms", "copyright", "©", "linkedin", "facebook",
    "twitter", "instagram", "youtube", "powered by", "version", "help",
    "language", "theme", "dark mode", "skip to",
)

#: Controls that almost always matter and are almost never recorded.
NOTABLE_CONTROL_WORDS = (
    "forgot", "reset password", "change password", "sign up", "register",
    "remember me", "export", "download", "import", "print", "audit", "history",
    "approve", "reject", "cancel", "delete", "activate", "deactivate",
    "bulk", "upload", "filter", "advanced search", "reset",
)


def triage_control(control: dict) -> str:
    """``notable`` / ``ignorable`` / ``unknown`` — no LLM involved.

    Deliberately conservative. "Forgot your password?" is the case this exists
    for: a control that plainly matters and that no recording ever touches,
    because the tester was recording a successful login.
    """
    blob = f"{control.get('label', '')} {control.get('selector', '')} " \
           f"{control.get('href', '')}".lower()
    if any(word in blob for word in NOTABLE_CONTROL_WORDS):
        return "notable"
    if any(word in blob for word in IGNORABLE_CONTROL_WORDS):
        return "ignorable"
    return "unknown"


def _suite_selectors(flow_path: Path) -> set[str]:
    """Every selector the recording touched or the suite references."""
    used: set[str] = set()
    for act in read_json(flow_path / "actions.json", []) or []:
        for candidate in [act.get("selector")] + list(act.get("selectors") or []):
            if candidate:
                used.add(str(candidate).strip())
    for case in coverage.expand_scenarios(read_json(flow_path / "test_cases.json", []) or []):
        for step in executor.parse_steps(case.get("steps") or []):
            selector = (step.get("selector") or "").strip()
            if selector:
                used.add(selector)
    return used


# ──────────────────────────────────────────────────────────────────────
# Defect log — an additional feature, deliberately isolated
#
# Nothing here writes to a flow's test_cases.json, so a normal Run, the coverage
# numbers and the existing report are exactly what they were. Defect scenarios
# get their own flow ("<Flow> [defects]") and therefore their own report.
# ──────────────────────────────────────────────────────────────────────
def list_flow_names(project_name: str) -> list[str]:
    """Every flow folder in a project, in name order."""
    flows_root = config.flows_dir(project_name)
    if not flows_root.exists():
        return []
    return sorted(p.name for p in flows_root.iterdir() if p.is_dir())


def _defect_context(project_name: str, flow_name: str) -> dict:
    """Everything needed to turn one module's defects into runnable steps."""
    flow_path = config.flow_dir(project_name, flow_name)
    actions = read_json(flow_path / "actions.json", []) or []
    return {
        "fields": _form_fields(project_name, flow_name),
        "add_selector": next(
            (a.get("selector") for a in actions
             if (a.get("action") or "").upper() == "CLICK"
             and any(word in f"{a.get('text', '')} {a.get('selector', '')}".lower()
                     for word in ("add", "new", "create"))), ""),
        "submit_selector": coverage_submit_selector(actions),
        "target_url": (read_json(flow_path / "flow.json", {}) or {}).get("target_url", ""),
        "actions": actions,
    }


def _build_defect_plan(project_name: str, records: list[dict]) -> dict:
    """Map every defect to a flow and to steps, reporting what is not runnable."""
    flow_names = list_flow_names(project_name)
    planned, blocked = [], []
    contexts: dict[str, dict] = {}

    for defect in records:
        flow = defects.match_flow(defect["module"], flow_names)
        if not flow:
            blocked.append({
                "defect": defect, "flow": "",
                "reason": (f"No recorded module matches {defect['module']!r}. Record "
                           f"that module first, or rename the flow to match."),
            })
            continue
        if flow not in contexts:
            contexts[flow] = _defect_context(project_name, flow)
        context = contexts[flow]
        built = defects.build_scenario(
            defect, context["fields"],
            add_selector=context["add_selector"],
            submit_selector=context["submit_selector"])
        built["flow"] = flow
        if built["runnable"]:
            planned.append(built)
        else:
            blocked.append({
                "defect": defect, "flow": flow,
                "reason": "; ".join(built["unresolved"]) or "no steps could be derived",
                "suggestions": built["suggestions"],
                "fields_known": len(context["fields"]),
            })
    return {"planned": planned, "blocked": blocked, "flows": sorted(contexts)}


def _write_defect_flows(project_name: str,
                        records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build one ``<Flow> [defects]`` flow per module. Returns (written, blocked).

    Every write goes to the defect flow. The real module's folder is only ever
    read, which is what keeps this feature disconnected from the main flow.
    """
    plan = _build_defect_plan(project_name, records)
    by_flow: dict[str, list[dict]] = {}
    for entry in plan["planned"]:
        by_flow.setdefault(entry["flow"], []).append(entry["scenario"])

    written: list[dict] = []
    for flow, scenarios in sorted(by_flow.items()):
        source = config.flow_dir(project_name, flow)
        target_name = defects.defect_flow_name(flow)
        target = config.flow_dir(project_name, target_name)
        assert_inside_projects(target)
        target.mkdir(parents=True, exist_ok=True)

        # Copy the recording metadata so auth, the target URL and the selector
        # fallbacks behave as they do for the real module.
        meta = read_json(source / "flow.json", {}) or {}
        write_json(target / "flow.json", {
            "flow_name": target_name,
            "target_url": meta.get("target_url", ""),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "step_count": 0,
            "_defects_for": flow,
        })
        write_json(target / "actions.json", read_json(source / "actions.json", []) or [])
        write_json(target / "test_cases.json", scenarios)
        written.append({"flow": target_name, "scenarios": len(scenarios)})

    blocked = [{"defect_id": b["defect"]["defect_id"],
                "module": b["defect"]["module"], "flow": b["flow"],
                "reason": b["reason"], "suggestions": b.get("suggestions") or {}}
               for b in plan["blocked"]]
    return written, blocked


@app.post("/preview-defect-import")
async def preview_defect_import(file: UploadFile = File(...),
                                project_name: str = config.DEFAULT_PROJECT):
    """Parse a defect sheet and show what would happen. Writes nothing."""
    require_name(project_name=project_name)
    try:
        records = defects.read_sheet(await file.read(), file.filename or "")
    except defects.DefectImportError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except Exception as err:
        raise HTTPException(status_code=400,
                            detail=f"Could not read the sheet: {err}") from err

    plan = _build_defect_plan(project_name, records)
    return {
        "total": len(records),
        "runnable": len(plan["planned"]),
        "needs_input": len(plan["blocked"]),
        "flows": plan["flows"],
        "planned": [{"defect_id": p["defect"]["defect_id"], "flow": p["flow"],
                     "scenario_name": p["scenario"]["scenario_name"],
                     "steps": p["scenario"]["steps"],
                     "expected_result": p["scenario"]["expected_result"],
                     "known_defect": p["scenario"]["_known_defect"]}
                    for p in plan["planned"]],
        "blocked": [{"defect_id": b["defect"]["defect_id"],
                     "module": b["defect"]["module"], "flow": b["flow"],
                     "reason": b["reason"],
                     "suggestions": b.get("suggestions") or {}}
                    for b in plan["blocked"]],
    }


@app.post("/import-defects")
async def import_defects(file: UploadFile = File(...),
                         project_name: str = config.DEFAULT_PROJECT):
    """Store the defect log and build a separate [defects] flow per module.

    The main suites are never touched: each defect flow is its own folder with
    its own ``test_cases.json``, so running it, or not running it, has no effect
    on the module's real results.
    """
    require_name(project_name=project_name)
    try:
        records = defects.read_sheet(await file.read(), file.filename or "")
    except defects.DefectImportError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    merged, added, updated = defects.merge(defects.load(project_name), records)
    defects.save(project_name, merged)

    # Plan from the merged log so any values accepted earlier are honoured — the
    # upload itself never carries them.
    written, blocked = _write_defect_flows(project_name, merged)

    runnable = sum(w["scenarios"] for w in written)
    db.audit("import_defects", project_name,
             details={"added": added, "updated": updated, "runnable": runnable})
    return {
        "status": "success",
        "stored": len(merged), "added": added, "updated": updated,
        "flows_written": written,
        "needs_input": blocked,
        "message": (
            f"{added} new and {updated} updated defect(s) stored. "
            f"{runnable} runnable scenario(s) written to {len(written)} separate "
            f"[defects] flow(s) — your existing suites are untouched."
            + (f" {len(blocked)} defect(s) need a value or a recording first."
               if blocked else "")
        ),
    }


@app.post("/save-defect-data")
async def save_defect_data(project_name: str = config.DEFAULT_PROJECT,
                           payload: dict = Body(...)):
    """Accept concrete values for a defect whose steps were left relative.

    The sheet says "a value less than or equal to the Inner Diameter"; this is
    where that becomes 40. Stored on the defect record so it survives a
    re-import, and applied by :func:`defects.build_scenario`.
    """
    require_name(project_name=project_name)
    defect_id = str(payload.get("defect_id") or "").strip()
    data = payload.get("data")
    if not defect_id or not isinstance(data, dict):
        raise HTTPException(status_code=400,
                            detail="Send {'defect_id': '...', 'data': {selector: value}}.")

    stored = defects.load(project_name)
    for record in stored:
        if record.get("defect_id") == defect_id:
            record["data"] = {str(k): str(v) for k, v in data.items() if str(k).strip()}
            break
    else:
        raise HTTPException(status_code=404,
                            detail=f"No imported defect with id {defect_id!r}.")
    defects.save(project_name, stored)
    return {"status": "success", "defect_id": defect_id,
            "message": "Values saved. Rebuild the defect flows to use them."}


@app.post("/rebuild-defect-flows")
async def rebuild_defect_flows(project_name: str = config.DEFAULT_PROJECT):
    """Regenerate the [defects] flows from the stored log."""
    require_name(project_name=project_name)
    stored = defects.load(project_name)
    if not stored:
        raise HTTPException(status_code=400,
                            detail="No defect log imported for this workspace yet.")
    written, blocked = _write_defect_flows(project_name, stored)
    return {"status": "success", "flows_written": written,
            "needs_input": blocked,
            "message": (f"{sum(w['scenarios'] for w in written)} scenario(s) across "
                        f"{len(written)} [defects] flow(s). Existing suites untouched.")}


@app.get("/defect-status")
async def defect_status(response: Response, project_name: str = config.DEFAULT_PROJECT):
    """Per defect: still open, fixed, or a regression.

    Computed here from each [defects] flow's own report, so the verdict engine
    keeps its PASS/FAIL meaning and nothing about the main report changes.
    """
    no_cache(response)
    require_name(project_name=project_name)
    log_rows = defects.load(project_name)

    latest: dict[str, dict] = {}
    for flow in list_flow_names(project_name):
        if not flow.endswith(defects.DEFECT_FLOW_SUFFIX):
            continue
        report = read_json(config.flow_dir(project_name, flow) / "report.json", {}) or {}
        for checkpoint in report.get("checkpoints", []):
            name = checkpoint.get("name") or ""
            found = re.match(r"\[([^\]]+)\]", name)
            if found:
                latest[found.group(1)] = checkpoint

    rows = []
    for defect in log_rows:
        checkpoint = latest.get(defect.get("defect_id"))
        rows.append({
            "defect_id": defect.get("defect_id"),
            "module": defect.get("module"),
            "priority": defect.get("priority"),
            "sheet_status": defect.get("status"),
            "last_result": (checkpoint or {}).get("status", "not run"),
            "outcome": (defects.outcome((checkpoint or {}).get("status", ""),
                                        bool(defect.get("is_open")))
                        if checkpoint else "not_run"),
            "reason": ((checkpoint or {}).get("verdict") or {}).get("reason", "")[:220],
        })
    tally: dict[str, int] = {}
    for row in rows:
        tally[row["outcome"]] = tally.get(row["outcome"], 0) + 1
    return {"defects": rows, "total": len(rows), "summary": tally}


# ──────────────────────────────────────────────────────────────────────
# Project export — a separate, strictly read-only feature
#
# It reads flow folders and returns generated files. It writes nothing: no flow
# folder is modified, no test_cases.json is touched, and neither the executor nor
# the verdict engine is involved. Removing this section would change nothing
# about how the platform records, generates or runs.
# ──────────────────────────────────────────────────────────────────────
def _export_modules(project_name: str, only_flow: str = "") -> list[dict]:
    """Gather each flow's recording, field inventory and scenarios. Read-only."""
    modules: list[dict] = []
    for flow in list_flow_names(project_name):
        if flow.endswith(defects.DEFECT_FLOW_SUFFIX):
            continue                      # defect flows are their own feature
        if only_flow and flow != only_flow:
            continue
        flow_path = config.flow_dir(project_name, flow)
        meta = read_json(flow_path / "flow.json", {}) or {}
        raw_cases = read_json(flow_path / "test_cases.json", []) or []
        modules.append({
            "flow": flow,
            "target_url": meta.get("target_url", ""),
            "actions": read_json(flow_path / "actions.json", []) or [],
            "fields": _form_fields(project_name, flow),
            # Expanded, so a scenario carrying a data table exports as the
            # concrete test cases a tester would actually run.
            "scenarios": coverage.expand_scenarios(raw_cases),
        })
    return modules


@app.get("/preview-export")
async def preview_export(response: Response, project_name: str = config.DEFAULT_PROJECT,
                         flow_name: str = ""):
    """The file tree the export would produce, plus one file to eyeball."""
    no_cache(response)
    require_name(project_name=project_name)
    modules = _export_modules(project_name, flow_name)
    if not modules:
        raise HTTPException(status_code=400,
                            detail="No recorded flows to export in this workspace.")

    files = export_project.build_project(project_name, modules)
    runnable = [m for m in modules if m["scenarios"]]
    return {
        "project": project_name,
        "modules": [{"flow": m["flow"], "scenarios": len(m["scenarios"]),
                     "fields": len(m["fields"]),
                     "exported": bool(m["scenarios"])} for m in modules],
        "files": [{"path": path, "bytes": len(content)}
                  for path, content in sorted(files.items())],
        "total_files": len(files),
        "total_scenarios": sum(len(m["scenarios"]) for m in runnable),
        "sample_path": next((p for p in sorted(files) if p.startswith("pages/")
                             and p != "pages/BasePage.js"), "pages/BasePage.js"),
        "sample": files.get(
            next((p for p in sorted(files) if p.startswith("pages/")
                  and p != "pages/BasePage.js"), "pages/BasePage.js"), "")[:4000],
        "skipped": [m["flow"] for m in modules if not m["scenarios"]],
    }


@app.get("/download-project")
async def download_project(project_name: str = config.DEFAULT_PROJECT,
                           flow_name: str = ""):
    """Stream the generated Playwright project as a zip. Writes nothing."""
    require_name(project_name=project_name)
    modules = _export_modules(project_name, flow_name)
    if not modules:
        raise HTTPException(status_code=400,
                            detail="No recorded flows to export in this workspace.")

    files = export_project.build_project(project_name, modules)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path, content in sorted(files.items()):
            bundle.writestr(path, content)
    buffer.seek(0)
    db.audit("export_project", project_name,
             details={"files": len(files), "modules": len(modules)})
    stem = f"{project_name}{'_' + flow_name if flow_name else ''}_playwright"
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem) + ".zip"
    return StreamingResponse(
        buffer, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ──────────────────────────────────────────────────────────────────────
# Wording corrections — close the loop on a guessed message literal
#
# A generated VERIFY_MESSAGE carries the wording a model imagined. The run then
# observes what the application really says. Rather than leaving the user to
# reconcile the two by hand, the mismatch is offered as a concrete edit.
# ──────────────────────────────────────────────────────────────────────
#: A toast reports its severity as its own element, so the scan sees "ERROR"
#: alongside the real text. Asserting on the severity would pass against *any*
#: error, which is barely a check at all.
SEVERITY_LABELS = {"error", "success", "warn", "warning", "info", "danger",
                   "fail", "failed", "ok"}


def _best_message_text(candidates: list[str]) -> str:
    """The most specific line among what the scan saw, or "" if none is useful.

    Toast frameworks surface three strings for one message — the severity, the
    detail, and the two joined — and only the detail is worth asserting on.
    """
    useful: list[str] = []
    for raw in candidates:
        text = str(raw or "").strip()
        if not text or text.lower() in SEVERITY_LABELS:
            continue
        if "\n" in text or "\r" in text:
            # The composite: keep the part that is not the severity.
            parts = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
            parts = [p for p in parts if p.lower() not in SEVERITY_LABELS]
            if parts:
                useful.append(max(parts, key=len))
            continue
        useful.append(text)
    if not useful:
        return ""
    # Most specific wins, but keep it short enough to be a stable assertion.
    return max(useful, key=len)[:120]


def _wording_fixes(project_name: str, flow_name: str) -> list[dict]:
    """Every step whose expected message differs from what the app showed."""
    flow_path = config.flow_dir(project_name, flow_name)
    report = read_json(flow_path / "report.json", {}) or {}
    cases = read_json(flow_path / "test_cases.json", []) or []
    by_name = {c.get("scenario_name"): c for c in cases}

    fixes: list[dict] = []
    for checkpoint in report.get("checkpoints", []):
        evidence = (checkpoint.get("verdict") or {}).get("evidence") or {}
        mismatches = list(evidence.get("wording_mismatches") or [])

        # Reports written before mismatches were recorded still hold the same
        # facts — a failed VERIFY_MESSAGE while messages were on screen — so the
        # corrections are recoverable from a run that has already happened.
        if not mismatches:
            seen = [m.get("text", "") for m in evidence.get("messages") or []]
            for assertion in evidence.get("assertions") or []:
                if (str(assertion.get("action") or "").upper() == "VERIFY_MESSAGE"
                        and not assertion.get("ok") and seen):
                    mismatches.append({"expected": assertion.get("value", ""),
                                       "actual": seen[:3]})

        for mismatch in mismatches:
            scenario = by_name.get(checkpoint.get("name"))
            if not scenario:
                continue
            expected = str(mismatch.get("expected") or "")
            actual = [a for a in (mismatch.get("actual") or []) if a]
            if not actual:
                continue
            # Prefer the most specific line: toast wrappers repeat the text with
            best = _best_message_text(actual)
            if not best:
                continue
            for index, step in enumerate(scenario.get("steps") or []):
                if str(step.get("action") or "").upper() != "VERIFY_MESSAGE":
                    continue
                if str(step.get("value") or "") != expected:
                    continue
                # A correction is only safe when it is genuinely about wording.
                # Two cases where it is not, and both must be visible:
                typed = [str(st.get("value") or "") for st in scenario.get("steps") or []
                         if str(st.get("action") or "").upper() == "TYPE"]
                # Compare against the untruncated text: a long input would
                # otherwise fall outside the shortened message and look clean.
                whole = " ".join(actual)
                data_coupled = any(v and len(v) > 3 and (v in best or v[:40] in whole)
                                   for v in typed)
                requirement = str(scenario.get("expected_result") or "")

                warning = ""
                if _literal_matched_the_requirement(expected, requirement, best):
                    warning = ("The wording this step expected reflects the behaviour "
                               "the scenario requires, and the application said "
                               "something unrelated — so the behaviour looks absent, "
                               "not merely worded differently. Applying this would "
                               "make the scenario pass forever without testing it.")
                elif data_coupled:
                    warning = ("This message repeats the value the scenario typed, so "
                               "asserting on it would only ever pass for that input. "
                               "Assert on the stable part of the sentence instead.")
                elif _contradicts_requirement(requirement, best):
                    warning = ("The application's message appears to contradict what "
                               "this scenario requires — that looks like a real defect, "
                               "not a wording mistake. Do not apply this without "
                               "reading it.")

                fixes.append({
                    "scenario": checkpoint.get("name"),
                    "step_index": index,
                    "expected": expected,
                    "actual": best,
                    "all_seen": actual,
                    "expected_result": requirement,
                    "warning": warning,
                    "safe": not warning,
                })
                break
    return fixes


#: Phrases that state a constraint on *what the message may say*. When the real
#: message violates one, the mismatch is a finding rather than a typo.
LEAK_REQUIREMENTS = (
    ("without giving away", "generic"), ("does not reveal", "generic"),
    ("without revealing", "generic"), ("generic error", "generic"),
    ("must not disclose", "generic"), ("without disclosing", "generic"),
)


STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "have", "has",
             "was", "are", "been", "not", "but", "you", "your", "its", "it",
             "message", "error", "shown", "displayed", "appears", "should"}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
            if w not in STOPWORDS}


def _literal_matched_the_requirement(expected_literal: str, requirement: str,
                                     actual: str) -> bool:
    """Was the step's wording an expression of the requirement, not a guess?

    "Too many attempts" against a requirement about rate limiting is the
    requirement restated; the application replying "Invalid Password" means the
    behaviour is missing. Rewriting the step to match would bury that finding.

    A literal the requirement never mentions — "Invalid credentials" against
    "an error message is displayed" — is just an invented spelling, and
    correcting it is safe.
    """
    literal_words = _content_words(expected_literal)
    if not literal_words:
        return False
    # If the step's wording echoes the requirement at all, the assertion is
    # carrying the requirement — so a mismatch is a finding until a human says
    # otherwise. Deliberately conservative: over-flagging costs a read, while
    # under-flagging silently rewrites a real defect into expected behaviour.
    return bool(literal_words & _content_words(requirement))


def _contradicts_requirement(requirement: str, actual: str) -> bool:
    """Does the observed message break a constraint the expectation states?

    Deliberately narrow. The case it exists for: an expectation asking for a
    *generic* error while the application names the account it could not find —
    applying that as a "fix" would bake a real security finding into the suite
    as the expected behaviour.
    """
    low = (requirement or "").lower()
    if not any(phrase in low for phrase, _ in LEAK_REQUIREMENTS):
        return False
    # A generic error does not name a specific account, field or identifier.
    return bool(re.search(r":\s*\S", actual or "")) or "username" in (actual or "").lower()


@app.get("/wording-fixes")
async def wording_fixes(response: Response, flow_name: str,
                        project_name: str = config.DEFAULT_PROJECT):
    """Steps whose expected wording the application does not use. Writes nothing."""
    no_cache(response)
    require_flow(project_name, flow_name)
    fixes = _wording_fixes(project_name, flow_name)
    return {"flow_name": flow_name, "count": len(fixes), "fixes": fixes}


@app.post("/apply-wording-fixes")
async def apply_wording_fixes(flow_name: str,
                              project_name: str = config.DEFAULT_PROJECT,
                              payload: dict = Body(default=None)):
    """Rewrite the chosen VERIFY_MESSAGE steps to the wording the app really uses."""
    flow_path = require_flow(project_name, flow_name)
    fixes = _wording_fixes(project_name, flow_name)
    if not fixes:
        raise HTTPException(status_code=400,
                            detail="No wording mismatches were recorded in the last run.")

    # Only the corrections that are genuinely about wording are applied without
    # being named. Anything carrying a warning must be chosen deliberately —
    # several of these "mismatches" are real defects the application is
    # reporting, and silently adopting them as expected behaviour would hide it.
    if payload and isinstance(payload.get("apply"), list):
        wanted = {(str(x.get("scenario")), int(x.get("step_index", -1)))
                  for x in payload["apply"]}
    else:
        wanted = {(f["scenario"], f["step_index"]) for f in fixes if f["safe"]}

    cases = read_json(flow_path / "test_cases.json", []) or []
    by_name = {c.get("scenario_name"): c for c in cases}
    applied = []
    for fix in fixes:
        if (fix["scenario"], fix["step_index"]) not in wanted:
            continue
        scenario = by_name.get(fix["scenario"])
        if not scenario:
            continue
        steps = scenario.get("steps") or []
        if fix["step_index"] >= len(steps):
            continue
        steps[fix["step_index"]]["value"] = fix["actual"]
        applied.append({"scenario": fix["scenario"], "from": fix["expected"],
                        "to": fix["actual"]})

    if applied:
        write_json(flow_path / "test_cases.json", cases)
        db.audit("apply_wording_fixes", f"{project_name}/{flow_name}",
                 details={"applied": len(applied)})
    skipped = [{"scenario": f["scenario"], "reason": f["warning"]}
               for f in fixes if f["warning"]
               and (f["scenario"], f["step_index"]) not in wanted]
    return {"status": "success", "applied": len(applied), "changes": applied,
            "skipped": skipped,
            "message": (f"{len(applied)} step(s) now expect the wording the "
                        f"application actually uses. Re-run to confirm."
                        + (f" {len(skipped)} left alone — read them first."
                           if skipped else ""))}


@app.get("/llm-status")
async def llm_status(response: Response, probe: bool = False):
    """Is the key valid, which model, how much of today's allowance is left.

    Answers the question the old error message sent people down the wrong path
    on — "check the API key" — when the key was fine and the daily quota was
    simply spent. ``probe=true`` lists the models the key can actually reach,
    which costs no generation quota.
    """
    no_cache(response)
    budget = llm.budget_status()
    payload = {
        "key_configured": bool(config.GEMINI_API_KEY),
        "model": config.GEMINI_MODEL,
        "vision_healing": config.VISION_HEALING_ENABLED,
        "max_retries": config.GEMINI_MAX_RETRIES,
        "usage": llm.usage_summary(),
        "budget": budget,
        "note": "",
    }
    if budget["exhausted"]:
        payload["note"] = llm.quota_message(budget["model"], "daily")

    if probe and config.GEMINI_API_KEY:
        try:
            payload["reachable_models"] = await llm.list_models()
            payload["model_available"] = config.GEMINI_MODEL in payload["reachable_models"]
        except Exception as err:
            payload["reachable_models"] = []
            payload["probe_error"] = str(err)[:300]
    return payload


@app.get("/page-coverage")
async def page_coverage(response: Response, flow_name: str,
                        project_name: str = config.DEFAULT_PROJECT,
                        mode: str = "headless"):
    """Which interactive controls on the page no test ever touches.

    The recording is a *lower bound* on what the application contains, and the
    platform used to treat it as the upper bound — so "Forgot your password?" was
    never tested, and nothing ever said so. This loads the real page, enumerates
    every control, and subtracts everything the recording and the suite mention.
    """
    no_cache(response)
    flow_path = require_flow(project_name, flow_name)
    meta = read_json(flow_path / "flow.json", {}) or {}
    target_url = executor.resolve_target_url(project_name, meta)
    if not target_url:
        raise HTTPException(status_code=400, detail="This flow has no target URL recorded.")

    used = _suite_selectors(flow_path)
    controls: list[dict] = []
    note = ""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=(mode != "headed"))
        try:
            context_args: dict[str, Any] = {}
            session_file = config.project_session_file(project_name)
            if session_file.exists():
                context_args["storage_state"] = str(session_file)
            context = await browser.new_context(**context_args)
            page = await context.new_page()
            page.set_default_timeout(8000)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await executor.wait_for_settle(page)
            if "login" in page.url.lower() and "login" not in flow_name.lower():
                note = ("Redirected to the login page, so this is the login screen's "
                        "inventory. Run the suite once to store a session, then re-check.")
            controls = await page.evaluate(CONTROL_INVENTORY_JS) or []
        except Exception as err:
            raise HTTPException(status_code=502,
                                detail=f"Could not load {target_url}: {err}") from err
        finally:
            await browser.close()

    exercised, uncovered = [], []
    for control in controls:
        selector = control["selector"]
        # A control counts as exercised if the suite names it, or names something
        # that contains it — a recorded `#loginBtn` covers `button#loginBtn`.
        touched = selector in used or any(
            selector in candidate or candidate in selector for candidate in used)
        if touched:
            exercised.append(control)
        else:
            control["verdict"] = triage_control(control)
            uncovered.append(control)

    order = {"notable": 0, "unknown": 1, "ignorable": 2}
    uncovered.sort(key=lambda c: order.get(c["verdict"], 1))
    return {
        "flow_name": flow_name,
        "target_url": target_url,
        "note": note,
        "total": len(controls),
        "exercised": len(exercised),
        "uncovered": uncovered,
        "notable": [c for c in uncovered if c["verdict"] == "notable"],
    }


@app.get("/coverage-report")
async def coverage_report(response: Response, flow_name: str,
                          project_name: str = config.DEFAULT_PROJECT):
    """How many scenarios, how many test cases, and which behaviours are missing."""
    no_cache(response)
    flow_path = require_flow(project_name, flow_name)
    cases = read_json(flow_path / "test_cases.json", []) or []
    summary = coverage.coverage_summary(cases)
    summary["fields_known"] = len(_form_fields(project_name, flow_name))
    summary["flow_name"] = flow_name
    return summary


@app.get("/preview-coverage-cases")
async def preview_coverage_cases(response: Response, flow_name: str,
                                 project_name: str = config.DEFAULT_PROJECT):
    """The enumerated field scenarios, without saving anything.

    Enumeration rather than generation: the cases come from each field's own
    type and constraints, so the same form always yields the same set and no
    API key is involved.
    """
    no_cache(response)
    flow_path = require_flow(project_name, flow_name)
    fields = _form_fields(project_name, flow_name)
    if not fields:
        raise HTTPException(
            status_code=400,
            detail="No fields are known for this module yet. Record the flow, or run "
                   "it once so the engine can inventory the form.")

    actions = read_json(flow_path / "actions.json", []) or []
    open_steps = coverage_open_steps(actions)
    submit = coverage_submit_selector(actions)
    scenarios = coverage.build_field_scenarios(
        fields, open_steps=open_steps, submit_selector=submit)
    return {
        "scenarios": scenarios,
        "scenario_count": len(scenarios),
        "test_case_count": sum(len(s["data_table"]) for s in scenarios),
        "submit_selector": submit,
        "open_steps": open_steps,
        "skipped_fields": [f.get("selector") for f in fields
                           if learned.is_sensitive(f)],
    }


def coverage_open_steps(actions: list) -> list[dict]:
    """The clicks that reach the form, taken from the recording up to the first TYPE."""
    steps: list[dict] = []
    for act in actions:
        action = (act.get("action") or "").upper()
        selector = (act.get("selector") or "").strip()
        if action == "TYPE":
            break
        if action == "CLICK" and selector:
            steps.append({"action": "CLICK", "selector": selector})
    return steps[:4]


def coverage_submit_selector(actions: list) -> str:
    """The submit control this module actually uses, else a labelled fallback."""
    for act in reversed(actions):
        selector = (act.get("selector") or "").strip()
        text = (act.get("text") or "").lower()
        if (act.get("action") or "").upper() != "CLICK" or not selector:
            continue
        if any(word in text for word in executor.SUBMIT_WORDS):
            return selector
    return 'button:has-text("Save")'


@app.post("/add-coverage-cases")
async def add_coverage_cases(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                             replace: bool = False):
    """Append the enumerated field scenarios to the suite.

    Appends by default: a generated suite is the tester's work and must not be
    silently replaced. Existing scenarios with the same name are skipped rather
    than duplicated.
    """
    flow_path = require_flow(project_name, flow_name)
    preview = await preview_coverage_cases(Response(), flow_name, project_name)
    scenarios = preview["scenarios"]

    existing = [] if replace else (read_json(flow_path / "test_cases.json", []) or [])
    names = {c.get("scenario_name") for c in existing}
    added = [s for s in scenarios if s["scenario_name"] not in names]
    write_json(flow_path / "test_cases.json", existing + added)
    db.audit("add_coverage_cases", f"{project_name}/{flow_name}",
             details={"scenarios": len(added),
                      "test_cases": sum(len(s["data_table"]) for s in added)})
    return {"status": "success", "added": len(added),
            "skipped": len(scenarios) - len(added),
            "test_cases": sum(len(s["data_table"]) for s in added),
            "total_scenarios": len(existing) + len(added)}


@app.get("/preview-duplicate-checks")
async def preview_duplicate_checks(response: Response, flow_name: str,
                                   project_name: str = config.DEFAULT_PROJECT):
    """The two-phase duplicate scenarios this flow's uniqueness key implies."""
    no_cache(response)
    flow_path = require_flow(project_name, flow_name)
    actions = read_json(flow_path / "actions.json", []) or []
    scenarios, notes = contracts.duplicate_scenarios(
        project_name, flow_name,
        open_steps=coverage_open_steps(actions),
        submit_selector=coverage_submit_selector(actions),
    )
    return {"scenarios": scenarios, "notes": notes, "count": len(scenarios)}


@app.post("/add-duplicate-checks")
async def add_duplicate_checks(flow_name: str,
                               project_name: str = config.DEFAULT_PROJECT):
    """Append the duplicate scenarios, de-duplicated by name."""
    flow_path = require_flow(project_name, flow_name)
    preview = await preview_duplicate_checks(Response(), flow_name, project_name)
    scenarios = preview["scenarios"]
    if not scenarios:
        raise HTTPException(
            status_code=400,
            detail=preview["notes"][0] if preview["notes"] else
            "Nothing to add — give this flow a 'unique_by' in its contract.")
    cases = read_json(flow_path / "test_cases.json", []) or []
    existing = {c.get("scenario_name") for c in cases}
    added = [s for s in scenarios if s["scenario_name"] not in existing]
    write_json(flow_path / "test_cases.json", cases + added)
    db.audit("add_duplicate_checks", f"{project_name}/{flow_name}",
             details={"scenarios": len(added)})
    return {"status": "success", "added": len(added),
            "skipped": len(scenarios) - len(added), "notes": preview["notes"]}


@app.post("/add-relationship-checks")
async def add_relationship_checks(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                                  dependencies: str = ""):
    """Append the generated relationship scenarios to the saved suite."""
    flow_path = require_flow(project_name, flow_name)
    scenarios, notes = contracts.relationship_checks(project_name, flow_name, dependencies)
    if not scenarios:
        raise HTTPException(
            status_code=400,
            detail="Nothing to add. " + (notes[0] if notes else
                                         "Give this flow a contract with a 'requires' entry."),
        )
    cases = read_json(flow_path / "test_cases.json", []) or []
    existing = {c.get("scenario_name") for c in cases}
    added = [s for s in scenarios if s["scenario_name"] not in existing]
    write_json(flow_path / "test_cases.json", cases + added)
    return {"status": "success", "added": len(added), "skipped": len(scenarios) - len(added),
            "notes": notes}


def _matrix_rows(project_name: str, flow_names: list[str]) -> list[dict]:
    """One row per **test case**: Module | Scenario | Test Case | Steps | Expected.

    A scenario states the objective; its test cases are the concrete data rows
    that cover it. Expanding here means the exported matrix is what a tester
    actually executes, and the Scenario column finally holds a scenario rather
    than a test case wearing its name.
    """
    rows: list[dict] = []
    for flow_name in flow_names:
        flow_path = config.flow_dir(project_name, flow_name)
        cases = coverage.expand_scenarios(read_json(flow_path / "test_cases.json", []) or [])
        contract = contracts.load(project_name, flow_name)
        report = read_json(flow_path / "report.json", {}) or {}
        last_status = {c.get("name"): c.get("status") for c in report.get("checkpoints", [])}

        requires = ", ".join(f"{r['entity']} (via {r['selector']})" for r in contract["requires"])
        base_precondition = "Logged in" + (f"; {requires} must exist" if requires else "")

        for index, case in enumerate(cases, start=1):
            steps = executor.parse_steps(case.get("steps") or [])
            rendered = []
            for number, step in enumerate(steps, start=1):
                action = step.get("action", "")
                detail = step.get("selector", "")
                if step.get("value"):
                    detail = f"{detail} = {step['value']}".strip(" =")
                rendered.append(f"{number}. {action} {detail}".strip())

            case_type = (case.get("type") or "").lower()
            if case_type.startswith("positive") or case_type in ("smoke", "functional"):
                priority = "P1"
            elif case_type in ("negative", "boundary", "security"):
                priority = "P2"
            else:
                priority = "P3"
            if case.get("_generated") == "rule":
                priority = "P1"          # a rule is something you already know matters

            rows.append({
                "Module": flow_name,
                "ID": f"{flow_name[:12].replace(' ', '_')}-{index:03d}",
                "Scenario": (case.get("_parent_name")
                             or case.get("scenario_name") or case.get("scenario")
                             or f"Case {index}"),
                "Test Case": (case.get("_case_name")
                              or case.get("scenario_name") or f"Case {index}"),
                "Data": ", ".join(f"{k}={v!r}" for k, v in
                                  (case.get("_data_row") or {}).items()),
                "Type": case.get("type", ""),
                "Pre-condition": case.get("precondition") or base_precondition,
                "Steps": "\n".join(rendered),
                "Expected Result": case.get("expected_result") or case.get("expected") or "",
                "Priority": priority,
                "Assertions": sum(1 for s in steps
                                  if str(s.get("action", "")).upper().startswith("VERIFY")),
                "Source": case.get("_generated") or "authored",
                "Last Run": last_status.get(case.get("scenario_name"), "not run"),
            })
    return rows


@app.get("/export-test-matrix")
async def export_test_matrix(project_name: str = config.DEFAULT_PROJECT, flow_name: str = "",
                             fmt: str = "csv"):
    """The Test Case Matrix a QA lead hands to a team or imports into TestRail.

    Covers one flow, or the whole project when ``flow_name`` is empty.
    """
    validate_safe_path(project_name, flow_name)
    if flow_name:
        require_flow(project_name, flow_name)
        flows = [flow_name]
    else:
        flows_root = config.flows_dir(project_name)
        flows = sorted(f.name for f in flows_root.iterdir() if f.is_dir()) \
            if flows_root.exists() else []
    rows = _matrix_rows(project_name, flows)
    if not rows:
        raise HTTPException(status_code=404, detail="No test cases found to export.")

    columns = ["ID", "Module", "Scenario", "Test Case", "Data", "Type",
               "Pre-condition", "Steps", "Expected Result", "Priority",
               "Assertions", "Source", "Last Run"]
    stem = f"test_matrix_{project_name}" + (f"_{flow_name}" if flow_name else "")

    if fmt == "markdown":
        lines = ["| " + " | ".join(columns) + " |",
                 "| " + " | ".join("---" for _ in columns) + " |"]
        for row in rows:
            cells = [str(row.get(c, "")).replace("\n", "<br>").replace("|", "\\|")
                     for c in columns]
            lines.append("| " + " | ".join(cells) + " |")
        body = (f"# Test Case Matrix — {project_name}\n\n"
                f"{len(rows)} test cases across {len(flows)} module(s), "
                f"generated {time.strftime('%Y-%m-%d %H:%M')}.\n\n" + "\n".join(lines) + "\n")
        return PlainTextResponse(body, headers={
            "Content-Disposition": f'attachment; filename="{stem}.md"'})

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{stem}.csv"'})


@app.get("/export-junit", response_class=PlainTextResponse)
async def export_junit(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                       report_file: str = ""):
    flow_path = require_flow(project_name, flow_name)
    report = read_json(flow_path / (report_file or "report.json"), None)
    if not report:
        raise HTTPException(status_code=404, detail="No report available to export.")
    return PlainTextResponse(executor.build_junit_xml(report), media_type="application/xml")


@app.get("/export-report-csv")
async def export_report_csv(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                            report_file: str = ""):
    flow_path = require_flow(project_name, flow_name)
    report = read_json(flow_path / (report_file or "report.json"), None)
    if not report:
        raise HTTPException(status_code=404, detail="No report available to export.")

    import csv as csv_module

    buffer = io.StringIO()
    writer = csv_module.writer(buffer)
    writer.writerow(["#", "Scenario", "Type", "Status", "Verdict reason", "Duration (s)",
                     "Failed step", "Expected result", "Messages observed"])
    for case in report.get("checkpoints", []):
        writer.writerow([
            case.get("index"), case.get("name"), case.get("type", ""), case.get("status"),
            (case.get("verdict") or {}).get("reason", "") or case.get("error", ""),
            case.get("duration", 0), case.get("failed_step", ""),
            case.get("expected", ""),
            " | ".join(m.get("text", "") for m in case.get("messages", [])),
        ])
    filename = f"{project_name}_{flow_name}_report.csv".replace(" ", "_")
    return Response(
        content=buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export-report-html", response_class=HTMLResponse)
async def export_report_html(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                             report_file: str = ""):
    """Self-contained printable report (use the browser's Print → Save as PDF)."""
    flow_path = require_flow(project_name, flow_name)
    report = read_json(flow_path / (report_file or "report.json"), None)
    if not report:
        raise HTTPException(status_code=404, detail="No report available to export.")

    def esc(text: Any) -> str:
        return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    colours = {"PASS": "#16a34a", "FAIL": "#dc2626", "UNVERIFIED": "#d97706",
               "CRASH": "#7c2d12", "CANCELLED": "#64748b"}
    rows = []
    for case in report.get("checkpoints", []):
        status = case.get("status", "")
        verdict_info = case.get("verdict") or {}
        shot = case.get("screenshot", "")
        rows.append(f"""
        <tr>
          <td>{esc(case.get('index'))}</td>
          <td><strong>{esc(case.get('name'))}</strong><br>
              <span class="muted">{esc(case.get('type', ''))}</span></td>
          <td><span class="badge" style="background:{colours.get(status, '#475569')}">
              {esc(status)}</span></td>
          <td>{esc(verdict_info.get('reason') or case.get('error', ''))}</td>
          <td>{esc(case.get('expected', ''))}</td>
          <td>{esc(case.get('duration', 0))}s</td>
          <td>{'<img src="' + esc(shot) + '" />' if shot else ''}</td>
        </tr>""")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(project_name)} / {esc(flow_name)} — QA Execution Report</title>
<style>
 body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#0f172a}}
 h1{{margin:0 0 4px}} .muted{{color:#64748b;font-size:12px}}
 .kpis{{display:flex;gap:12px;margin:18px 0}}
 .kpi{{border:1px solid #e2e8f0;border-radius:10px;padding:12px 18px;min-width:110px}}
 .kpi b{{display:block;font-size:22px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th,td{{border:1px solid #e2e8f0;padding:8px;text-align:left;vertical-align:top}}
 th{{background:#f8fafc}}
 .badge{{color:#fff;padding:3px 8px;border-radius:20px;font-size:11px;font-weight:700}}
 img{{max-width:220px;border:1px solid #e2e8f0;border-radius:6px}}
 @media print{{ img{{max-width:150px}} }}
</style></head><body>
<h1>{esc(project_name)} — {esc(flow_name)}</h1>
<div class="muted">Executed {esc(report.get('execution_date'))} ·
 mode {esc(report.get('mode'))} · verdict engine {esc(report.get('verdict_mode', 'n/a'))} ·
 environment {esc(report.get('environment') or 'default')} ·
 triggered by {esc(report.get('triggered_by', 'dashboard'))}</div>
<div class="kpis">
 <div class="kpi"><b>{report.get('total_test_cases', 0)}</b>Total</div>
 <div class="kpi" style="color:#16a34a"><b>{report.get('passed_count', 0)}</b>Passed</div>
 <div class="kpi" style="color:#dc2626"><b>{report.get('failed_count', 0)}</b>Failed</div>
 <div class="kpi" style="color:#d97706"><b>{report.get('unverified_count', 0)}</b>Unverified</div>
 <div class="kpi" style="color:#7c2d12"><b>{report.get('crash_count', 0)}</b>Crashed</div>
 <div class="kpi"><b>{report.get('duration_seconds', 0)}s</b>Duration</div>
</div>
<table><thead><tr><th>#</th><th>Scenario</th><th>Status</th><th>Verdict</th>
<th>Expected</th><th>Time</th><th>Evidence</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="muted">Unverified means every step ran but nothing on screen proved the expected
outcome — add a VERIFY step to make the scenario provable.</p>
</body></html>"""
    return HTMLResponse(html)


@app.get("/download-evidence")
async def download_evidence(flow_name: str, project_name: str = config.DEFAULT_PROJECT):
    """Zip the latest report plus screenshots, videos and traces for sharing."""
    flow_path = require_flow(project_name, flow_name)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in ("report.json", "junit.xml", "test_cases.json", "flow.json"):
            candidate = flow_path / name
            if candidate.exists():
                bundle.write(candidate, name)
        for folder in ("screenshots", "videos", "traces", "downloads"):
            source = flow_path / folder
            if not source.exists():
                continue
            for item in source.rglob("*"):
                if item.is_file():
                    bundle.write(item, f"{folder}/{item.relative_to(source)}")
    buffer.seek(0)
    filename = f"{project_name}_{flow_name}_evidence.zip".replace(" ", "_")
    return StreamingResponse(
        buffer, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ──────────────────────────────────────────────────────────────────────
# API pre-seeding (Pillar 3)
# ──────────────────────────────────────────────────────────────────────
@app.get("/list-api-seeds")
async def list_api_seeds(project_name: str, response: Response):
    validate_safe_path(project_name)
    no_cache(response)
    data = api_seed.load_seeds(project_name)
    return {"seeds": sorted((data.get("seeds") or {}).keys()), "definitions": data.get("seeds")}


@app.post("/save-api-seed")
async def save_api_seed(project_name: str, seed_name: str, payload: dict = Body(...)):
    validate_safe_path(project_name)
    if not payload.get("requests"):
        raise HTTPException(status_code=400,
                            detail="A seed definition needs at least one entry in 'requests'.")
    api_seed.save_seed(project_name, seed_name, payload)
    return {"status": "success", "message": f"API seed '{seed_name}' saved."}


@app.post("/delete-api-seed")
async def delete_api_seed(project_name: str, seed_name: str):
    validate_safe_path(project_name)
    if not api_seed.delete_seed(project_name, seed_name):
        raise HTTPException(status_code=404, detail="Seed not found.")
    return {"status": "success", "message": f"API seed '{seed_name}' deleted."}


@app.post("/seed-prerequisite-api")
async def seed_prerequisite_api(project_name: str, seed_name: str):
    """Run a registered seed immediately (Pillar 3 — now actually issues requests)."""
    validate_safe_path(project_name)
    try:
        pool = await api_seed.run_seed(project_name, seed_name)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except Exception as err:
        raise HTTPException(status_code=502, detail=str(err)) from err
    return {"status": "success", "variables": pool,
            "message": f"Seed '{seed_name}' executed — {len(pool)} variable(s) captured."}


# ──────────────────────────────────────────────────────────────────────
# Scheduling
# ──────────────────────────────────────────────────────────────────────
@app.get("/list-schedules")
async def list_schedules(project_name: str, response: Response):
    validate_safe_path(project_name)
    no_cache(response)
    return {"schedules": scheduler.load_schedules(project_name),
            "scheduler_running": scheduler.is_running()}


@app.post("/save-schedule")
async def save_schedule(project_name: str, payload: dict = Body(...)):
    validate_safe_path(project_name)
    try:
        saved = scheduler.save_schedule(project_name, payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return {"status": "success", "schedule": saved, "message": "Schedule saved."}


@app.post("/delete-schedule")
async def delete_schedule(project_name: str, schedule_id: str):
    validate_safe_path(project_name)
    if not scheduler.delete_schedule(project_name, schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return {"status": "success", "message": "Schedule removed."}


# ──────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    llm.configure()
    db.init()
    log.info("Workspace: %s", config.BASE_DIR)
    log.info("AI configured: %s (model %s)", bool(config.GEMINI_API_KEY), config.GEMINI_MODEL)
    log.info("Authentication: %s", "enabled" if config.API_KEY else "disabled (local only)")
    if config.SCHEDULER_ENABLED:
        scheduler.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    scheduler.stop()
    executor.cancel_all_runs()
    await close_active_browser()
