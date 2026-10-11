"""Safe file persistence for the on-disk data model.

``projects/`` is the database, and several writers touch it at once: a run
persisting its report, the scheduler, parallel workers teaching ``learned.json``,
and the dashboard saving a suite. Two things go wrong without this module:

- **Torn files.** A plain ``open(path, "w")`` truncates first, so a crash or a
  concurrent reader sees an empty or half-written file. Every write here goes to
  a uniquely named temp file in the same directory and is swapped in with
  ``os.replace``, which is atomic on both POSIX and Windows.
- **Lost updates.** ``run_history.json`` and ``bugs.json`` are read, changed and
  written back. Two runs finishing together each read the old list and the second
  write silently drops the first run. ``update_json`` holds a per-file lock across
  the whole read-modify-write.

Locks are per process. The platform runs as one server process (the run
registry and scheduler already assume that), so a thread lock is sufficient.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("qa_agent.storage")

_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}

# Windows refuses os.replace while another handle has the target open (a reader,
# an antivirus scan). The window is milliseconds, so a short retry clears it.
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF = 0.05


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def lock_for(path: Path | str) -> threading.RLock:
    """The lock guarding ``path``. Re-entrant, so a locked caller may write."""
    key = _key(path)
    with _registry_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@contextmanager
def locked(path: Path | str) -> Iterator[None]:
    with lock_for(path):
        yield


def _replace(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))


def write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``text``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with lock_for(target):
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                   dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def write_json(path: Path | str, payload: Any, indent: int = 4) -> None:
    """Atomically replace ``path`` with ``payload`` serialised as JSON."""
    write_text(path, json.dumps(payload, indent=indent))


def read_json(path: Path | str, default: Any = None) -> Any:
    """Parsed JSON, or ``default`` when the file is missing or unreadable."""
    target = Path(path)
    try:
        with lock_for(target):
            with open(target, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception as err:
        log.warning("Unreadable JSON %s: %s", target, err)
        return default


class _Box:
    """Holds the document inside ``update_json`` so the caller may replace it."""

    __slots__ = ("data",)

    def __init__(self, data: Any) -> None:
        self.data = data


@contextmanager
def update_json(path: Path | str, default: Any = None, indent: int = 4) -> Iterator[_Box]:
    """Read, let the caller change, and write back under one lock.

    ``with update_json(p, []) as doc: doc.data.append(x)`` — assign
    ``doc.data`` to replace the document outright. Nothing is written if the
    block raises.
    """
    target = Path(path)
    with lock_for(target):
        current = read_json(target, None)
        if current is None:
            current = default() if callable(default) else json.loads(json.dumps(default))
        box = _Box(current)
        yield box
        write_json(target, box.data, indent=indent)
