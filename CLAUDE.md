# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local AI QA automation platform: record a browser flow once, have Gemini agents
generate a test suite from it, replay it with Playwright, and judge each scenario
against evidence collected from the live page. FastAPI backend + a single-page
vanilla-JS dashboard. There is no database of record — `projects/` on disk is the
data model.

## Commands

```powershell
# Run the server (frees port 8000 first — a raw uvicorn call fails with
# WinError 10048 if a previous server still holds the port)
.\run_server.ps1                 # -Port 8080  -Reload  -Address 0.0.0.0
run_server.bat                   # CMD equivalent
python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000   # equivalent to app.main:app

# Tests
.venv\Scripts\python.exe -m pytest tests -q                    # unit (63)
.venv\Scripts\python.exe -m pytest tests/test_core.py -q       # one file
.venv\Scripts\python.exe -m pytest tests -q -k verdict         # one pattern
.venv\Scripts\python.exe tests\e2e_selftest.py                 # real browser, 13 verdict cases
.venv\Scripts\python.exe tests\smoke_ui.py                     # boots server, drives dashboard
.venv\Scripts\python.exe tests\integration_api.py              # CI endpoints, auth, cancellation
.venv\Scripts\python.exe tests\regression_existing_data.py     # backward-compat vs real projects/
.venv\Scripts\python.exe tests\e2e_chain.py                    # dependency chain (WIP, failing)

# Records a narrated walkthrough video of the dashboard (needs a running server)
.venv\Scripts\python.exe tests\record_walkthrough.py --project ornage --flow Login
```

Only `test_core.py` and `test_platform.py` are pytest files. The others are
standalone scripts that spin up their own server/browser and exit non-zero on
failure; CI runs all of them (`.github/workflows/tests.yml`).

## Architecture

### Request → verdict pipeline

The core idea, and the thing that spans the most files: **a scenario is not a pass
just because no step threw.** Understanding one run means reading these in order:

1. `app/main.py` builds a `RunConfig` and calls `executor.execute_suite`, streaming
   progress over SSE.
2. `app/executor.py` runs each scenario in its own browser context, dispatching
   steps (`execute_step`) and assertions (`run_assertion`), while accumulating a
   `verdict.Evidence` object — on-screen messages, native dialogs, table rows,
   downloads, console errors, assertion outcomes, captured variables.
3. `app/verdict.py` classifies the scenario's `expected_result` into an intent
   (expects_error / success / absence / persistence / sanitised / stability) and
   matches it against that evidence, producing **PASS / FAIL / UNVERIFIED / CRASH**.
   `UNVERIFIED` means "everything ran but nothing proved it" — deliberately not a pass.
4. `app/rules.py` then evaluates the project's rules against the same evidence and
   can override a pass into a FAIL.
5. `build_report` / `persist_report` write `report.json`, a timestamped copy,
   `run_history.json`, per-project `bugs.json`, and a row in SQLite.

Changing verdict behaviour almost always means touching `verdict.py` (classification)
*and* `executor.py` (what evidence gets collected).

### Workspace resolution

`app/config.py` resolves everything once: `BASE_DIR` is `QA_WORKSPACE` if set,
otherwise the repo root (one level above `app/`). All paths — `projects/`, `logs/`,
`.trash/`, the SQLite file — derive from it. Never hardcode paths; use
`config.flow_dir()`, `config.project_dir()`, `config.static_url()`.

`qa_agent.py` at the root is a two-line shim (`from app.main import app`) that keeps
`uvicorn qa_agent:app` working for existing launchers and docs.

### On-disk data model

```
projects/<project>/config.json          environments, credentials, roles, notifications
projects/<project>/rules.json           project-wide rules
projects/<project>/bugs.json
projects/<project>/flows/<flow>/
    flow.json actions.json test_cases.json    recording + suite
    contract.json rules.json                  dependency contract, flow-scoped rules
    report.json run_history.json reports/     results
    screenshots/ videos/ traces/ downloads/   evidence (gitignored)
```

`projects/` is the user's real test data. Deletes go through `move_to_trash()`
(soft delete into `.trash/`), never `rmtree`.

### Web layer — the important constraint

`web/app.js` is the original dashboard logic, extracted **verbatim**. Do not edit it.
`web/ui.js` loads afterwards and only wraps its public functions to change
presentation. This matters because:

- `app.js` declares its state with `let` at top level (`loadedTestCases`,
  `currentFlowName`, …), which is **not** reachable on `window`. Replacing a render
  function on `window` orphans that state and silently breaks the editor/filter —
  wrap with the `wrap()` helper in `ui.js` instead.
- `app.js` reveals panels with inline `style.display`, so `.hidden` in `styles.css`
  must not use `!important`.
- `web/.id-contract.txt` lists the 133 element ids the logic requires. Any markup
  change must keep them; `tests/smoke_ui.py` is what catches a break.

The pre-redesign single-file UI is preserved at `web/legacy/` and served at `/legacy`.

### Tester-knowledge layers

Two things a recording cannot capture, both feeding the same run:

- **`app/contracts.py`** — a flow declares what it `provides` (an entity + the field
  whose value to capture into `{{var}}`) and what it `requires` (an entity + the
  dropdown that consumes it). This drives run-once seeding of masters, named
  variable capture across modules, and generated relationship checks
  (active master appears / selecting it saves / inactive excluded / delete-in-use blocked).
- **`app/rules.py`** — invariants written in plain English, translated by one LLM call
  into structured rules that the user reviews before saving. Each rule is enforced on
  every scenario, injected into the Builder/Breaker/Auditor prompts, and can generate
  its own scenario.

### AI layer

`app/llm.py` wraps Gemini: prefers the maintained `google-genai` SDK, falls back to
the end-of-life `google-generativeai`, retries with backoff, tracks approximate token
usage, trims oversized prompts. `app/ai_agents.py` holds the Builder / Breaker /
Auditor prompts and the shared step vocabulary. All AI is optional — execution,
verdicts and reporting work with no API key.

### Run registry

`executor.register_run()` returns a `RunState` kept in a module-level dict. It powers
the Stop button (`/stop-run` sets `cancelled`, which each scenario checks), CI polling
(`/trigger-run` → `/run-status/{id}`), and the scheduler.

## Gotchas

- **Windows async**: `qa_agent.py` sets `WindowsProactorEventLoopPolicy` before
  anything imports Playwright. Standalone test scripts must do the same.
- **Console encoding**: `config.py` reconfigures stdout/stderr to UTF-8; without it a
  single emoji in a log line raises `UnicodeEncodeError` on cp1252 consoles. Ad-hoc
  scripts that print report content need the same `reconfigure` call.
- **Destructive endpoints**: `validate_safe_path()` skips empty strings, so an empty
  `project_name` would resolve to the projects root. Anything that deletes or renames
  must also use `require_name()` and `assert_inside_projects()`.
- **SQLite joins**: `runs` and `results` share `epoch`, `project_name`, `flow_name` —
  qualify every column or SQLite raises "ambiguous column name" (which the `except`
  blocks would swallow into an empty list).
- **Test isolation**: every test sets `QA_WORKSPACE` to a temp dir *before* importing
  `config`. The one exception is `regression_existing_data.py`, which runs against the
  real workspace and must stay strictly read-only (and keeps `QA_SCHEDULER=0`).
- **Selector fallbacks**: `heuristics.looks_like_dismiss()` is word-aware on purpose. A
  plain substring test once treated the letter `x` as "close", so a failed click on
  `#taxCode` fell through to the Cancel button and "passed" against the wrong element.
- **False failures**: when a scenario reports "no validation message appeared", check
  `MESSAGE_SCAN_JS` in `executor.py` before trusting it — the app may be fine and the
  scanner simply blind to that framework's error class.

## Current state

All six suites pass: 81 unit tests, `e2e_selftest` 13/13, `e2e_chain` 18/18,
`smoke_ui` 32/32, `integration_api` 32/32, `regression_existing_data` 18/18.

### Handling application quirks without hand-fixing

A recorded selector is one literal spelling of one control, and a real screen
defeats it in predictable ways. Each of these is handled in the engine, so the
same test case keeps working without anyone editing it — `chain.html` section 7
reproduces all of them and `e2e_chain` proves them:

- **A closed dialog left in the DOM ahead of the open one.** `p-dialog` used to
  resolve to the invisible first match ("exists but is not visible"). Both
  `first_usable()` and the assertion `locator()` now scan matches for one that is
  actually on screen; `FORM_INVENTORY_JS` and `EMPTY_REQUIRED_JS` pick a *visible*
  container, without which the form inventory described a dialog nobody could see
  and self-completion silently did nothing.
- **The dialog wins outright.** `first_usable()` tries *every* candidate scoped to
  the open dialog before trying any of them page-wide. Candidate-by-candidate
  scoping was not enough: a recorded `button:has-text("Save")` matched a Save
  button on the page behind the dialog rather than the dialog's own "Update".
- **Different words for the same control.** `heuristics.synonym_selectors()`
  expands Save↔Update↔Submit, No↔Cancel↔Close, Export↔Download plus icon-only
  buttons (`button:has(i.pi-file-excel)`, `[title*=…]`). Reject synonyms never
  include confirm words — offering "Yes" for "No" would pass the wrong test.
- **A widget that refuses the keystrokes.** A numeric field dropping letters *is*
  the application validating, at the input layer. `note_input_rejection()` records
  it and verdict branch 2c resolves the expectation, replacing the backwards
  "the invalid input appears to have been accepted".
- **Empty-grid wording.** "No data available" satisfies an assertion written as
  "No records found" (`EMPTY_STATE_PHRASES`) — a framework difference is not a
  defect.
- **`VERIFY_READONLY` against a probe selector.** When `input[readonly]` matches
  nothing, `READONLY_SCAN_JS` reports which fields are locked, or that none are —
  which is itself the finding, not a selector error.

`column_not_empty` is a rule kind, so "audit history always shows date and user"
is stated once and enforced on every scenario.

Rules, contracts, the Test Case Matrix, per-role logins and the extra run flags
(`seed_once`, `relationship_checks`, `apply_rules`) all have both endpoints and UI —
the UI lives in `web/ui-rules.js`, which wraps `app.js` the same way `ui.js` does.

Still open:

- **Concurrency testing** — nothing exercises two users racing on the same action
  (e.g. simultaneous code generation). Would need a scenario type that runs the same
  steps in two contexts at once.
- **Test-data cleanup** — repeated end-to-end runs against a real ERP accumulate
  records and flood approval queues. There is no reset/purge strategy.
- **QR-scan style steps** are untestable until an application exposes a non-camera
  path; flagged as a design-for-testability requirement rather than a platform gap.
- `projects/ornage` targets the public OrangeHRM demo. Its `Login` flow has a working
  6-scenario suite; `Admin` has no suite and its recording uses fragile structural
  selectors (`div > div > form > div:nth-of-type(2) …`) that predate multi-candidate
  selector capture.
- Still on the legacy `google-generativeai` SDK; `pip install google-genai` makes
  `llm.py` switch automatically.
