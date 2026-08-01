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
from . import db
from . import executor
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
    """Persist one recorded interaction plus a step screenshot."""
    if not active_session["recording"]:
        return
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
def build_fields_context(actions: list) -> str:
    fields: list[str] = []
    for act in actions:
        selector = act.get("selector", "")
        tag = act.get("tag", "")
        text = act.get("text", "") or act.get("label", "")
        action_type = (act.get("action") or "").upper()
        if not selector:
            continue
        if action_type in ("TYPE", "SELECT") or tag in ("input", "select", "textarea"):
            fields.append(f"Field: {selector} (tag: {tag}, label: {text})")
        elif action_type == "CLICK" and text:
            fields.append(f"Button/Link: {selector} (text: {text})")
    return "\n".join(fields) if fields else "No specific fields detected from recording."


@app.post("/analyze-flow")
async def analyze_flow_api(flow_name: str, project_name: str = config.DEFAULT_PROJECT,
                           test_types: str = ""):
    flow_path = require_flow(project_name, flow_name)
    actions = read_json(flow_path / "actions.json", []) or []
    meta = read_json(flow_path / "flow.json", {}) or {}
    if not meta.get("target_url"):
        raise HTTPException(status_code=400, detail="This flow has no target URL recorded.")

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
    db.audit("analyze_flow", f"{project_name}/{flow_name}",
             details={"cases": len(analysis.get("test_cases", []))})

    return {"status": "success",
            "message": "AI analysis complete. Enterprise QA Specification generated successfully!"}


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
        actions_json = llm.trim_for_prompt(json.dumps(actions, indent=2))
        fields_context = build_fields_context(actions)

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
            for agent_no, result in enumerate(results, start=1):
                if isinstance(result, Exception):
                    yield sse({"event": "agent_error", "agent": agent_no,
                               "message": f"⚠️ Agent {agent_no} error: {result}"})
                    agent_cases.append([])
                elif isinstance(result, list):
                    agent_cases.append([c for c in result if isinstance(c, dict)])
                elif isinstance(result, dict) and isinstance(result.get("test_cases"), list):
                    agent_cases.append(result["test_cases"])
                else:
                    agent_cases.append([])

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
                yield sse({"event": "agent_done", "agent": agent_no, "count": len(cases),
                           "unique_added": added,
                           "message": f"✅ Agent {agent_no} generated {len(cases)} {label} "
                                      f"test cases ({added} unique)."})

            yield sse({"event": "merge", "total": len(all_cases),
                       "message": f"📋 Merged suite: {len(all_cases)} total unique test cases."})

            if not all_cases:
                yield sse({"event": "error",
                           "message": "No test cases were produced. Check the API key, the "
                                      "model name and the server log, then retry."})
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
        write_json(flow_path / "test_cases.json", all_cases)
        write_json(flow_path / "generation_meta.json", {
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
        fields_context = build_fields_context(actions)
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
    """One row per scenario: Module | Scenario | Pre-condition | Steps | Expected | Priority."""
    rows: list[dict] = []
    for flow_name in flow_names:
        flow_path = config.flow_dir(project_name, flow_name)
        cases = read_json(flow_path / "test_cases.json", []) or []
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
                "Scenario": case.get("scenario_name") or case.get("scenario") or f"Case {index}",
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

    columns = ["ID", "Module", "Scenario", "Type", "Pre-condition", "Steps",
               "Expected Result", "Priority", "Assertions", "Source", "Last Run"]
    stem = f"test_matrix_{project_name}" + (f"_{flow_name}" if flow_name else "")

    if fmt == "markdown":
        lines = ["| " + " | ".join(columns) + " |",
                 "| " + " | ".join("---" for _ in columns) + " |"]
        for row in rows:
            cells = [str(row.get(c, "")).replace("\n", "<br>").replace("|", "\\|")
                     for c in columns]
            lines.append("| " + " | ".join(cells) + " |")
        body = (f"# Test Case Matrix — {project_name}\n\n"
                f"{len(rows)} scenarios across {len(flows)} module(s), "
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
