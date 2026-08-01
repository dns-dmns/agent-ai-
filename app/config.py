"""Central configuration, workspace paths, and logging for the QA platform.

Replaces the hardcoded ``D:/Ai`` paths that used to be scattered through
``qa_agent.py``. The workspace root is resolved once, here:

1. ``QA_WORKSPACE`` environment variable, if set (useful for Docker / CI).
2. Otherwise the directory containing this file.

Everything else (projects, session state, credentials, database, trash) is
derived from that root, so the platform now runs from any folder on any OS.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────────────────────────────
# Workspace paths
# ──────────────────────────────────────────────────────────────────────

# Windows consoles default to cp1252, which raises UnicodeEncodeError as soon as
# a log line or SSE message contains an emoji. Force UTF-8 with replacement so a
# cosmetic character can never take the server down.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# The workspace root is the repository root — one level above this package —
# so projects/, logs/ and the database sit next to app/ rather than inside it.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
BASE_DIR: Path = Path(os.getenv("QA_WORKSPACE") or REPO_ROOT)
PROJECTS_DIR: Path = BASE_DIR / "projects"
TRASH_DIR: Path = BASE_DIR / ".trash"
TEMP_DIR: Path = BASE_DIR / ".tmp"
LOG_DIR: Path = BASE_DIR / "logs"
DB_PATH: Path = Path(os.getenv("QA_DB_PATH") or (BASE_DIR / "qa_platform.db"))

# Legacy global state files (kept for backward compatibility; per-project
# equivalents under projects/<name>/ take precedence when they exist).
GLOBAL_SESSION_FILE: Path = BASE_DIR / "session.json"
GLOBAL_LOGIN_DATA_FILE: Path = BASE_DIR / "login_data.json"

DEFAULT_PROJECT = os.getenv("QA_DEFAULT_PROJECT", "ERP_Automation")


def web_dir() -> Path:
    """Folder holding dashboard.html, styles.css, app.js and ui.js."""
    for candidate in (REPO_ROOT / "web", BASE_DIR / "web"):
        if (candidate / "dashboard.html").exists():
            return candidate
    return REPO_ROOT / "web"


def dashboard_file(name: str = "dashboard.html") -> Path:
    """Locate a UI file.

    Looked up at call time so a workspace override (Docker, tests) and older
    installs that keep dashboard.html in the root both keep working.
    """
    for candidate in (REPO_ROOT / "web" / name,
                      BASE_DIR / "web" / name,
                      REPO_ROOT / name,
                      BASE_DIR / name):
        if candidate.exists():
            return candidate
    return REPO_ROOT / "web" / name

for _d in (PROJECTS_DIR, TEMP_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def project_dir(project_name: str) -> Path:
    return PROJECTS_DIR / project_name


def flows_dir(project_name: str) -> Path:
    return PROJECTS_DIR / project_name / "flows"


def flow_dir(project_name: str, flow_name: str) -> Path:
    return PROJECTS_DIR / project_name / "flows" / flow_name


def project_session_file(project_name: str, role: str = "") -> Path:
    """Per-project (and optionally per-role) storage state.

    Roles get their own file so a run can hold a creator and an approver
    session side by side without one overwriting the other.
    """
    if role:
        safe = "".join(ch for ch in role if ch.isalnum() or ch in "-_") or "role"
        return project_dir(project_name) / f"session.{safe}.json"
    per_project = project_dir(project_name) / "session.json"
    if per_project.exists():
        return per_project
    return per_project if not GLOBAL_SESSION_FILE.exists() else GLOBAL_SESSION_FILE


def project_config_file(project_name: str) -> Path:
    return project_dir(project_name) / "config.json"


def static_url(project_name: str, flow_name: str, *parts: str) -> str:
    """Build the /static URL for a file inside a flow folder."""
    tail = "/".join(str(p).strip("/") for p in parts if p)
    return f"/static/projects/{project_name}/flows/{flow_name}/{tail}"


# ──────────────────────────────────────────────────────────────────────
# AI configuration
# ──────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", GEMINI_MODEL)
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
# Hard cap on how much recorded-action JSON is pasted into a prompt.
PROMPT_ACTIONS_CHAR_LIMIT = int(os.getenv("QA_PROMPT_ACTIONS_LIMIT", "24000"))

# ──────────────────────────────────────────────────────────────────────
# Runtime behaviour toggles
# ──────────────────────────────────────────────────────────────────────
API_KEY = os.getenv("QA_API_KEY", "")  # empty ⇒ authentication disabled
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "QA_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    ).split(",")
    if o.strip()
]
VISION_HEALING_ENABLED = os.getenv("QA_VISION_HEALING", "1") not in ("0", "false", "False")
VISION_MIN_CONFIDENCE = float(os.getenv("QA_VISION_MIN_CONFIDENCE", "0.6"))
MAX_PARALLEL_WORKERS = int(os.getenv("QA_MAX_WORKERS", "5"))
SCHEDULER_ENABLED = os.getenv("QA_SCHEDULER", "1") not in ("0", "false", "False")

# Words that mark an action as destructive — Vision AI coordinate clicking is
# never allowed on these, because a mis-located click would mutate real data.
DESTRUCTIVE_KEYWORDS = (
    "delete",
    "remove",
    "submit",
    "confirm",
    "save",
    "yes",
    "approve",
    "cancel order",
    "deactivate",
    "purge",
    "drop",
)


# ──────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────
def get_logger(name: str = "qa_platform") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(os.getenv("QA_LOG_LEVEL", "INFO").upper())
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        file_handler = logging.FileHandler(LOG_DIR / "qa_platform.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except Exception:  # pragma: no cover - read-only disk, etc.
        pass

    logger.propagate = False
    return logger


log = get_logger()
