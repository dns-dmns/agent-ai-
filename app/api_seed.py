"""Pillar 3 — Fast API / database pre-seeding, for real this time.

The previous implementation only stored a JSON template and never issued a
request, which made the roadmap claim misleading. This module actually executes
the registered requests, captures values out of the responses, and hands them
back to the executor as ``{{token}}`` variables — so a target flow can start
immediately on its own screen instead of replaying four prerequisite UI flows.

Seed definitions live in ``projects/<project>/api_seed.json``::

    {
      "seeds": {
        "master_data": {
          "base_url": "https://erp.example.com",
          "auth": {"type": "login", "path": "/api/auth/login",
                   "json": {"username": "admin", "password": "1234"},
                   "token_path": "data.token", "header": "Authorization",
                   "prefix": "Bearer "},
          "requests": [
            {"name": "category", "method": "POST", "path": "/api/v1/category",
             "json": {"name": "CAT_{{run_token}}", "status": "Active"},
             "capture": {"category_id": "data.id", "category_name": "data.name"}}
          ]
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import requests

from . import config
from . import heuristics

log = config.get_logger("api_seed")

Emitter = Callable[[str], Awaitable[None]] | None


def seed_file(project_name: str) -> Path:
    return config.project_dir(project_name) / "api_seed.json"


def load_seeds(project_name: str) -> dict:
    path = seed_file(project_name)
    if not path.exists():
        return {"seeds": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("seeds", {})
        return data
    except Exception as err:
        log.warning("Could not read api_seed.json for %s: %s", project_name, err)
        return {"seeds": {}}


def save_seed(project_name: str, name: str, definition: dict) -> dict:
    path = seed_file(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_seeds(project_name)
    data["seeds"][name] = definition
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
    return data


def delete_seed(project_name: str, name: str) -> bool:
    data = load_seeds(project_name)
    if name not in data.get("seeds", {}):
        return False
    data["seeds"].pop(name, None)
    with open(seed_file(project_name), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
    return True


def _extract(payload: Any, dotted_path: str) -> Any:
    """Minimal dotted-path lookup with numeric index support ("data.items.0.id")."""
    current = payload
    for part in (dotted_path or "").split("."):
        if part == "":
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except Exception:
                return None
        if isinstance(current, dict):
            if part in current:
                current = current[part]
                continue
            return None
        return None
    return current


def _cookies_from_session(project_name: str) -> dict:
    """Reuse the Playwright storage state so seeding hits an authenticated API."""
    session = config.project_session_file(project_name)
    if not session.exists():
        return {}
    try:
        with open(session, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        return {c.get("name"): c.get("value") for c in state.get("cookies", [])
                if c.get("name")}
    except Exception:
        return {}


def _resolve(value: Any, pool: dict) -> Any:
    if isinstance(value, str):
        return heuristics.resolve_dynamic_variables(value, pool)
    if isinstance(value, dict):
        return {k: _resolve(v, pool) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, pool) for v in value]
    return value


def _request(method: str, url: str, *, headers: dict, json_body: Any,
             params: dict | None, cookies: dict, timeout: int, verify: bool):
    return requests.request(
        method.upper(), url, headers=headers, json=json_body, params=params,
        cookies=cookies, timeout=timeout, verify=verify,
    )


async def run_seed(project_name: str, seed_name: str, emit: Emitter = None,
                   extra_pool: dict | None = None) -> dict:
    """Execute one seed definition and return the captured variable pool."""

    async def say(message: str) -> None:
        log.info(message)
        if emit:
            await emit(message)

    data = load_seeds(project_name)
    definition = (data.get("seeds") or {}).get(seed_name)
    if not definition:
        raise ValueError(f"API seed '{seed_name}' is not registered for {project_name}.")

    pool: dict = {"run_token": time.strftime("%H%M%S") + uuid.uuid4().hex[:3]}
    pool.update(extra_pool or {})

    base_url = (definition.get("base_url") or "").rstrip("/")
    timeout = int(definition.get("timeout_seconds", 20))
    verify = bool(definition.get("verify_tls", True))
    headers = {"Content-Type": "application/json", **(definition.get("headers") or {})}
    cookies = _cookies_from_session(project_name) if definition.get(
        "use_session_cookies", True) else {}

    # ── Authentication ────────────────────────────────────────────────
    auth = definition.get("auth") or {}
    if auth.get("type") == "login":
        login_url = auth.get("url") or f"{base_url}{auth.get('path', '')}"
        await say(f"🔐 [API Seed] Authenticating against {login_url}")
        response = await asyncio.to_thread(
            _request, auth.get("method", "POST"), login_url,
            headers=headers, json_body=_resolve(auth.get("json") or {}, pool),
            params=None, cookies=cookies, timeout=timeout, verify=verify,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"API seed login failed with HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        token = _extract(payload, auth.get("token_path", "token"))
        if token:
            headers[auth.get("header", "Authorization")] = (
                f"{auth.get('prefix', 'Bearer ')}{token}"
            )
            pool["api_token"] = token
            await say("🔐 [API Seed] Token acquired.")
        cookies.update(response.cookies.get_dict())
    elif auth.get("type") == "bearer" and auth.get("token"):
        headers[auth.get("header", "Authorization")] = (
            f"{auth.get('prefix', 'Bearer ')}{auth['token']}"
        )
    elif auth.get("type") == "header" and auth.get("headers"):
        headers.update(auth["headers"])

    # ── Seed requests ─────────────────────────────────────────────────
    started = time.time()
    for step_no, spec in enumerate(definition.get("requests") or [], start=1):
        url = spec.get("url") or f"{base_url}{spec.get('path', '')}"
        url = heuristics.resolve_dynamic_variables(url, pool)
        method = spec.get("method", "POST")
        body = _resolve(spec.get("json"), pool)
        params = _resolve(spec.get("params"), pool)

        await say(f"⚡ [API Seed {step_no}] {method} {url}")
        try:
            response = await asyncio.to_thread(
                _request, method, url, headers=headers, json_body=body,
                params=params, cookies=cookies, timeout=timeout, verify=verify,
            )
        except Exception as err:
            if spec.get("optional"):
                await say(f"⚠️ [API Seed {step_no}] optional request failed: {err}")
                continue
            raise RuntimeError(f"API seed request {step_no} failed: {err}") from err

        if response.status_code >= 400 and not spec.get("optional"):
            raise RuntimeError(
                f"API seed request {step_no} returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
        except Exception:
            payload = {}

        for var_name, path in (spec.get("capture") or {}).items():
            value = _extract(payload, path)
            if value is not None:
                pool[var_name] = value
                await say(f"📌 [API Seed] Captured {{{{{var_name}}}}} = {value}")
        if spec.get("name") and isinstance(body, dict):
            for key, value in body.items():
                pool.setdefault(f"{spec['name']}_{key}", value)

    elapsed = int((time.time() - started) * 1000)
    await say(f"✅ [API Seed] '{seed_name}' completed in {elapsed} ms — "
              f"{len(pool)} variable(s) available to the suite.")
    return pool
