"""AI QA Automation Platform — backend package.

Module map (import order roughly reflects dependency order):

* :mod:`app.config`      workspace paths, environment configuration, logging
* :mod:`app.llm`         Gemini wrapper: retry, usage tracking, prompt trimming
* :mod:`app.verdict`     decides PASS / FAIL / UNVERIFIED from on-screen evidence
* :mod:`app.heuristics`  idle waits, variable substitution, self-healing
* :mod:`app.recorder`    injected JS recorder (multi-candidate selectors)
* :mod:`app.ai_agents`   Builder / Breaker / Auditor prompts + step vocabulary
* :mod:`app.api_seed`    API/DB pre-seeding (Pillar 3)
* :mod:`app.db`          SQLite run history, flaky detection, audit log
* :mod:`app.executor`    Playwright execution engine
* :mod:`app.scheduler`   in-process scheduler + webhook notifier
* :mod:`app.main`        FastAPI application (the ``app`` object)

The FastAPI instance is intentionally *not* imported here: doing so would pull
Playwright and the Gemini SDK into every ``from app import config``. Use
``from app.main import app`` (or the root ``qa_agent.py`` shim).
"""

__version__ = "2.0.0"
