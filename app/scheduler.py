"""Zero-dependency scheduler and webhook notifier.

Runs inside the FastAPI process as a single asyncio task that wakes every 30
seconds, so nightly regression runs need no extra service (no APScheduler, no
cron, no Celery). Schedules live in ``projects/<project>/schedules.json``::

    {"schedules": [{
        "id": "a1b2c3",
        "name": "Nightly regression",
        "flow_name": "customer master",
        "enabled": true,
        "trigger": {"type": "daily", "at": "02:00"},
        "config": {"mode": "headless", "workers": 2, "dependencies": "login",
                   "verdict_mode": "rules"}
    }]}

Trigger types: ``daily`` (``at`` HH:MM), ``weekly`` (``day`` mon…sun + ``at``),
``interval`` (``minutes``), and ``once`` (``at`` ISO timestamp).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from . import config
from . import storage

log = config.get_logger("scheduler")

_task: asyncio.Task | None = None
_stopping = False
CHECK_INTERVAL_SECONDS = 30
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def schedules_file(project_name: str) -> Path:
    return config.project_dir(project_name) / "schedules.json"


def load_schedules(project_name: str) -> list[dict]:
    path = schedules_file(project_name)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("schedules", []) if isinstance(data, dict) else list(data)
    except Exception as err:
        log.warning("Could not read schedules for %s: %s", project_name, err)
        return []


def _write_schedules(project_name: str, schedules: list[dict]) -> None:
    path = schedules_file(project_name)
    storage.write_json(path, {"schedules": schedules})


def save_schedule(project_name: str, payload: dict) -> dict:
    if not payload.get("flow_name"):
        raise ValueError("A schedule needs a flow_name.")
    trigger = payload.get("trigger") or {}
    if trigger.get("type") not in ("daily", "weekly", "interval", "once"):
        raise ValueError("trigger.type must be daily, weekly, interval or once.")
    if trigger["type"] in ("daily", "weekly", "once") and not trigger.get("at"):
        raise ValueError("This trigger type needs an 'at' time (HH:MM).")
    if trigger["type"] == "interval" and not trigger.get("minutes"):
        raise ValueError("An interval trigger needs 'minutes'.")

    schedules = load_schedules(project_name)
    entry = {
        "id": payload.get("id") or uuid.uuid4().hex[:8],
        "name": payload.get("name") or f"{payload['flow_name']} run",
        "flow_name": payload["flow_name"],
        "enabled": bool(payload.get("enabled", True)),
        "trigger": trigger,
        "config": payload.get("config") or {},
        "created_at": payload.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_run": payload.get("last_run", ""),
        "last_status": payload.get("last_status", ""),
        "last_run_id": payload.get("last_run_id", ""),
    }
    replaced = False
    for index, existing in enumerate(schedules):
        if existing.get("id") == entry["id"]:
            entry["created_at"] = existing.get("created_at", entry["created_at"])
            entry["last_run"] = existing.get("last_run", "")
            entry["last_status"] = existing.get("last_status", "")
            schedules[index] = entry
            replaced = True
            break
    if not replaced:
        schedules.append(entry)
    _write_schedules(project_name, schedules)
    return entry


def delete_schedule(project_name: str, schedule_id: str) -> bool:
    schedules = load_schedules(project_name)
    remaining = [s for s in schedules if s.get("id") != schedule_id]
    if len(remaining) == len(schedules):
        return False
    _write_schedules(project_name, remaining)
    return True


def _mark_run(project_name: str, schedule_id: str, status: str, run_id: str = "") -> None:
    schedules = load_schedules(project_name)
    for entry in schedules:
        if entry.get("id") == schedule_id:
            entry["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            entry["last_status"] = status
            if run_id:
                entry["last_run_id"] = run_id
            break
    _write_schedules(project_name, schedules)


def is_due(entry: dict, now: datetime | None = None) -> bool:
    """Should this schedule fire in the current window?"""
    if not entry.get("enabled", True):
        return False
    now = now or datetime.now()
    trigger = entry.get("trigger") or {}
    kind = trigger.get("type")
    last_run_text = entry.get("last_run") or ""
    last_run: datetime | None = None
    if last_run_text:
        try:
            last_run = datetime.strptime(last_run_text, "%Y-%m-%d %H:%M:%S")
        except Exception:
            last_run = None

    if kind == "interval":
        minutes = max(1, int(trigger.get("minutes", 60)))
        if not last_run:
            return True
        return now - last_run >= timedelta(minutes=minutes)

    if kind in ("daily", "weekly"):
        try:
            hour, minute = [int(part) for part in str(trigger.get("at", "02:00")).split(":")[:2]]
        except Exception:
            return False
        if kind == "weekly":
            wanted = str(trigger.get("day", "mon")).lower()[:3]
            if WEEKDAYS[now.weekday()] != wanted:
                return False
        scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < scheduled_today:
            return False
        # Fire once per scheduled slot.
        return not last_run or last_run < scheduled_today

    if kind == "once":
        if last_run:
            return False
        try:
            target = datetime.fromisoformat(str(trigger.get("at")))
        except Exception:
            return False
        return now >= target

    return False


async def notify(project_name: str, report: dict, extra_note: str = "") -> None:
    """POST a Slack-compatible payload when a scheduled/CI run finishes."""
    cfg_path = config.project_config_file(project_name)
    if not cfg_path.exists():
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            project_cfg = json.load(fh)
    except Exception:
        return

    notifications = project_cfg.get("notifications") or {}
    url = notifications.get("webhook_url")
    if not url:
        return

    failed = report.get("failed_count", 0) + report.get("crash_count", 0)
    when = notifications.get("on", "always")
    if when == "failures_only" and failed == 0:
        return

    status_icon = "❌" if failed else ("⚠️" if report.get("unverified_count") else "✅")
    text = (
        f"{status_icon} *{report.get('project_name')} / {report.get('flow_name')}* — "
        f"{report.get('passed_count', 0)} passed, {failed} failed, "
        f"{report.get('unverified_count', 0)} unverified "
        f"({report.get('total_test_cases', 0)} total) in "
        f"{report.get('duration_seconds', 0)}s."
    )
    if extra_note:
        text += f"\n{extra_note}"

    payload: dict[str, Any] = {
        "text": text,
        "project": report.get("project_name"),
        "flow": report.get("flow_name"),
        "passed": report.get("passed_count", 0),
        "failed": report.get("failed_count", 0),
        "unverified": report.get("unverified_count", 0),
        "crashed": report.get("crash_count", 0),
        "run_id": report.get("run_id"),
        "executed_at": report.get("execution_date"),
    }
    try:
        await asyncio.to_thread(
            requests.post, url, json=payload,
            timeout=int(notifications.get("timeout_seconds", 10)),
        )
        log.info("Notification sent for %s/%s", report.get("project_name"),
                 report.get("flow_name"))
    except Exception as err:
        log.warning("Notification failed: %s", err)


async def _run_scheduled(project_name: str, entry: dict) -> None:
    from . import executor  # imported late to avoid a circular import at module load

    overrides = dict(entry.get("config") or {})
    overrides.pop("project_name", None)
    overrides.pop("flow_name", None)
    allowed = {
        "num_cases", "mode", "test_types", "auth_flow", "auth_scenario", "dependencies",
        "case_ids", "workers", "verdict_mode", "step_shots", "tracing", "dep_unique_data",
        "data_matrix", "visual_check", "env_name", "api_seed",
    }
    cfg = executor.RunConfig(
        project_name=project_name,
        flow_name=entry["flow_name"],
        triggered_by=f"schedule:{entry.get('name', entry['id'])}",
        **{k: v for k, v in overrides.items() if k in allowed},
    )
    state = executor.register_run(project_name, entry["flow_name"])
    _mark_run(project_name, entry["id"], "running", state.run_id)
    log.info("Scheduled run starting: %s / %s", project_name, entry["flow_name"])

    async def emit(_event: dict) -> None:
        return None

    try:
        report = await executor.execute_suite(cfg, emit, state)
        failed = report.get("failed_count", 0) + report.get("crash_count", 0)
        _mark_run(project_name, entry["id"], "failed" if failed else "passed", state.run_id)
        await notify(project_name, report,
                     extra_note=f"Triggered by schedule '{entry.get('name')}'.")
    except Exception as err:
        log.exception("Scheduled run failed")
        _mark_run(project_name, entry["id"], f"error: {err}"[:120], state.run_id)


async def _loop() -> None:
    log.info("Scheduler started (checking every %ss).", CHECK_INTERVAL_SECONDS)
    while not _stopping:
        try:
            if config.PROJECTS_DIR.exists():
                for project in config.PROJECTS_DIR.iterdir():
                    if not project.is_dir() or project.name.startswith("."):
                        continue
                    for entry in load_schedules(project.name):
                        if is_due(entry):
                            asyncio.create_task(_run_scheduled(project.name, entry))
        except Exception as err:
            log.warning("Scheduler tick failed: %s", err)
        for _ in range(CHECK_INTERVAL_SECONDS):
            if _stopping:
                break
            await asyncio.sleep(1)
    log.info("Scheduler stopped.")


def start() -> None:
    global _task, _stopping
    if _task and not _task.done():
        return
    _stopping = False
    _task = asyncio.create_task(_loop())


def stop() -> None:
    global _stopping
    _stopping = True
    if _task and not _task.done():
        _task.cancel()


def is_running() -> bool:
    return bool(_task and not _task.done())
