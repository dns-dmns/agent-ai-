"""Compatibility entry point.

The backend now lives in the :mod:`app` package (``app/main.py``), but every
existing launcher, script, guide and bookmark refers to ``qa_agent:app`` — so
this shim keeps all of them working unchanged:

    python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000   # still valid
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # new form

Both commands serve the identical application object.
"""

from app.main import app  # noqa: F401  (re-exported for uvicorn)

__all__ = ["app"]
