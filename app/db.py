"""SQLite history store.

JSON files stay the source of truth for flows and suites (nothing about the
existing workflow changes). This database is an *additional* index over run
results so the platform can answer questions the file tree cannot:

* when did this scenario last fail?
* which scenarios flip between pass and fail (flaky)?
* what is the pass-rate trend for this flow?

Every write is best-effort: if the database is unavailable the run still
completes and the JSON report is still written.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

log = config.get_logger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    project_name     TEXT NOT NULL,
    flow_name        TEXT NOT NULL,
    execution_date   TEXT NOT NULL,
    epoch            INTEGER NOT NULL,
    duration_seconds INTEGER,
    mode             TEXT,
    workers          INTEGER,
    verdict_mode     TEXT,
    environment      TEXT,
    triggered_by     TEXT,
    total            INTEGER,
    passed           INTEGER,
    failed           INTEGER,
    unverified       INTEGER,
    crashed          INTEGER,
    cancelled        INTEGER
);

CREATE TABLE IF NOT EXISTS results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    project_name   TEXT NOT NULL,
    flow_name      TEXT NOT NULL,
    scenario_index INTEGER,
    scenario_name  TEXT,
    scenario_type  TEXT,
    status         TEXT,
    verdict_source TEXT,
    verdict_reason TEXT,
    duration       INTEGER,
    failed_step    INTEGER,
    error          TEXT,
    epoch          INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE INDEX IF NOT EXISTS idx_results_scenario
    ON results (project_name, flow_name, scenario_name);
CREATE INDEX IF NOT EXISTS idx_results_status ON results (status);
CREATE INDEX IF NOT EXISTS idx_runs_flow ON runs (project_name, flow_name, epoch);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch     INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    actor     TEXT,
    action    TEXT NOT NULL,
    target    TEXT,
    details   TEXT
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    try:
        with connect() as conn:
            conn.executescript(SCHEMA)
    except Exception as err:
        log.warning("Could not initialise the database: %s", err)


def record_run(report: dict) -> None:
    """Index one completed run and all of its scenario results."""
    epoch = int(time.time())
    run_id = report.get("run_id") or f"run_{epoch}"
    try:
        with connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, project_name, flow_name, execution_date, epoch,
                    duration_seconds, mode, workers, verdict_mode, environment,
                    triggered_by, total, passed, failed, unverified, crashed, cancelled)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, report.get("project_name", ""), report.get("flow_name", ""),
                    report.get("execution_date", ""), epoch,
                    report.get("duration_seconds", 0), report.get("mode", ""),
                    report.get("workers", 1), report.get("verdict_mode", ""),
                    report.get("environment", ""), report.get("triggered_by", ""),
                    report.get("total_test_cases", 0), report.get("passed_count", 0),
                    report.get("failed_count", 0), report.get("unverified_count", 0),
                    report.get("crash_count", 0), 1 if report.get("cancelled") else 0,
                ),
            )
            rows = []
            for case in report.get("checkpoints", []):
                verdict = case.get("verdict") or {}
                rows.append((
                    run_id, report.get("project_name", ""), report.get("flow_name", ""),
                    case.get("index"), case.get("name"), case.get("type", ""),
                    case.get("status"), verdict.get("source", ""),
                    (verdict.get("reason") or "")[:600], case.get("duration", 0),
                    case.get("failed_step", 0), (case.get("error") or "")[:600], epoch,
                ))
            if rows:
                conn.executemany(
                    """INSERT INTO results
                       (run_id, project_name, flow_name, scenario_index, scenario_name,
                        scenario_type, status, verdict_source, verdict_reason, duration,
                        failed_step, error, epoch)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
    except Exception as err:
        log.warning("record_run failed: %s", err)


def audit(action: str, target: str = "", actor: str = "local", details: Any = None) -> None:
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (epoch, ts, actor, action, target, details) "
                "VALUES (?,?,?,?,?,?)",
                (int(time.time()), time.strftime("%Y-%m-%d %H:%M:%S"), actor, action,
                 target, json.dumps(details) if details is not None else ""),
            )
    except Exception:
        pass


def recent_runs(project_name: str = "", flow_name: str = "", limit: int = 50) -> list[dict]:
    query = "SELECT * FROM runs"
    clauses, params = [], []
    if project_name:
        clauses.append("project_name = ?")
        params.append(project_name)
    if flow_name:
        clauses.append("flow_name = ?")
        params.append(flow_name)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY epoch DESC LIMIT ?"
    params.append(limit)
    try:
        with connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]
    except Exception as err:
        log.warning("recent_runs failed: %s", err)
        return []


def scenario_history(project_name: str, flow_name: str, scenario_name: str,
                     limit: int = 20) -> list[dict]:
    try:
        with connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    # Both tables carry epoch/project_name/flow_name, so every
                    # column here must be qualified or SQLite raises "ambiguous
                    # column name".
                    """SELECT results.run_id      AS run_id,
                              results.status      AS status,
                              results.verdict_reason AS verdict_reason,
                              results.duration    AS duration,
                              results.epoch       AS epoch,
                              runs.execution_date AS execution_date,
                              runs.mode           AS mode
                       FROM results JOIN runs ON runs.run_id = results.run_id
                       WHERE results.project_name = ? AND results.flow_name = ?
                         AND results.scenario_name = ?
                       ORDER BY results.epoch DESC, results.id DESC LIMIT ?""",
                    (project_name, flow_name, scenario_name, limit),
                )
            ]
    except Exception as err:
        # Logged rather than swallowed silently: an ambiguous-column bug hid
        # here once already because the empty list looked like "no history".
        log.warning("scenario_history failed: %s", err)
        return []


def flaky_scenarios(project_name: str = "", window: int = 5,
                    min_flips: int = 2) -> list[dict]:
    """Scenarios whose status flips back and forth across recent runs."""
    query = """SELECT id, project_name, flow_name, scenario_name, status, epoch
               FROM results"""
    params: list = []
    if project_name:
        query += " WHERE project_name = ?"
        params.append(project_name)
    # id breaks ties: several runs recorded within the same second would
    # otherwise come back in arbitrary order, and since flakiness is measured
    # from the SEQUENCE of statuses, the verdict itself became non-deterministic.
    query += " ORDER BY epoch DESC, id DESC"
    try:
        with connect() as conn:
            rows = [dict(r) for r in conn.execute(query, params)]
    except Exception as err:
        log.warning("flaky_scenarios failed: %s", err)
        return []

    grouped: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        key = (row["project_name"], row["flow_name"], row["scenario_name"])
        bucket = grouped.setdefault(key, [])
        if len(bucket) < window:
            bucket.append(row["status"])

    flaky: list[dict] = []
    for (project, flow, scenario), statuses in grouped.items():
        if len(statuses) < 3:
            continue
        simple = ["PASS" if s == "PASS" else "NOTPASS" for s in statuses]
        flips = sum(1 for a, b in zip(simple, simple[1:]) if a != b)
        if flips >= min_flips:
            flaky.append({
                "project_name": project,
                "flow_name": flow,
                "scenario_name": scenario,
                "recent_statuses": statuses,
                "flips": flips,
            })
    return sorted(flaky, key=lambda item: item["flips"], reverse=True)


def stats(project_name: str = "") -> dict:
    try:
        with connect() as conn:
            where = " WHERE project_name = ?" if project_name else ""
            params = (project_name,) if project_name else ()
            runs = conn.execute(f"SELECT COUNT(*) AS c FROM runs{where}", params).fetchone()["c"]
            results = conn.execute(
                f"SELECT COUNT(*) AS c FROM results{where}", params
            ).fetchone()["c"]
            by_status = {
                row["status"]: row["c"]
                for row in conn.execute(
                    f"SELECT status, COUNT(*) AS c FROM results{where} GROUP BY status", params
                )
            }
            return {"runs": runs, "results": results, "by_status": by_status}
    except Exception:
        return {"runs": 0, "results": 0, "by_status": {}}


init()
