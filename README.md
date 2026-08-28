# 🤖 Multi-Project Enterprise AI QA Automation Platform

Record a browser flow once, let a team of AI agents generate the test suite, then
replay it with Playwright and get a report that is **actually verified** — not just
"no step threw an exception".

```
Record (Tab 1)  →  Generate (Tab 2: Builder + Breaker + Auditor)  →  Execute  →  Verdict + Report (Tab 3)
```

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows
pip install -r requirements.txt
playwright install chromium

copy .env.example .env                 # then paste your GEMINI_API_KEY
python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Better still, use a launcher that frees the port
for you — **`.\run_server.ps1`** in PowerShell or **`run_server.bat`** in CMD.
A raw `uvicorn` command fails with WinError 10048 if a previous server is still
holding the port. Execution, reporting and verdicts work without an API key —
only AI *generation* needs one.

The workspace is the folder containing `qa_agent.py`; override with
`QA_WORKSPACE` in `.env`. Nothing is hardcoded to a drive letter any more.

---

## The verdict engine (read this first)

A scenario used to be marked **PASS** whenever no Playwright step raised. Every
negative, boundary and security test therefore "passed" simply by typing bad data
and clicking Save — nothing checked that the validation the test expected ever
appeared.

Runs now produce four outcomes:

| Status | Meaning |
| :--- | :--- |
| **PASS** | The expectation was confirmed by evidence: an assertion passed, the expected validation appeared, a success toast fired, a file downloaded, or the URL changed as described. |
| **FAIL** | The expectation was contradicted — a negative test where no validation appeared, a positive test that raised an error, a failed assertion, or an injected payload that actually executed. |
| **UNVERIFIED** | Every step ran, but nothing proved the outcome. Not a pass. Add a `VERIFY_*` step (or turn on AI adjudication) to make the scenario provable. |
| **CRASH** | The browser died during the scenario. The suite continues. |

Pick the mode under **4. Verdict & Performance Options**:

* `rules` (default) — deterministic, evidence based.
* `ai` — rules first, then one Gemini call with the final screenshot for anything
  the rules could not settle.
* `off` — the old "steps ran ⇒ pass" behaviour, if you need to compare with
  historical reports.

Expect previously green suites to show FAIL/UNVERIFIED the first time. That is
the point: those results were never proven.

---

## Dependency chains and your own rules

Two things a recording can never tell the platform: what *you* know about the
application, and how its modules depend on each other.

### Contracts — what a flow provides and requires

`projects/<p>/flows/<f>/contract.json` turns "replay some clicks first" into real
integration testing:

```json
{ "provides": [{"entity": "category", "var": "category_name",
                "name_field": "#catName", "status_field": "#catStatus",
                "list_url": "/masters/category"}],
  "requires": [{"entity": "category", "selector": "#categoryDropdown"}],
  "run_once": true }
```

With that in place the engine:

* seeds `run_once` masters **once per run** instead of before every scenario,
* **captures** what the prerequisite created into `{{category_name}}`, usable in
  any later step — including inside selectors, e.g. `tr:has-text('{{order_code}}')`,
* generates the four cross-module checks you cannot express per-module: the active
  master appears downstream, selecting it saves, an **inactive master is excluded**,
  and a master in use **cannot be deleted**.

Anything it cannot generate is explained rather than skipped silently.

### Rules — your knowledge, enforced every run

Write them in plain English — **Rules** in the sidebar. One Gemini call turns them
into structured rules that **you review before anything is saved**:

> *After saving a Fabric Category it must stay on /fabric-category.*
> *Beam Length must reject 0 and negative values.*
> *The Excel export must contain every record, not just the visible page.*

Each rule then does three jobs:

1. **Enforced** on every scenario — a violation fails the run even when the
   scenario passes on its own terms, and every rule reports its own line.
2. **Fed into generation** — the Builder, Breaker and Auditor all receive them,
   and the Auditor treats an uncovered rule as a coverage gap.
3. **Turned into test cases** — a rule marked `generate_scenario` produces its own
   scenario, so an invariant you describe once is exercised directly.

Kinds: `must_show` · `must_not_show` · `must_navigate` · `must_not_navigate` ·
`field_rejects` · `field_accepts` · `export_complete` · `no_console_errors` ·
`row_persists`.

### Test Case Matrix

`Test matrix` on the report (or `GET /export-test-matrix?fmt=csv|markdown`) exports
**Module | Scenario | Pre-condition | Steps | Expected Result | Priority** for one
flow or the whole workspace — ready for TestRail, Zephyr or Excel.

## Recording — what gets captured

Recording is not just "what was clicked". Every event carries the element's
computed ARIA **role**, its **accessible name**, its **full visible text** and the
text-bearing attributes (`title`, `placeholder`, `aria-label`, `href`, `alt`…),
plus an exact `text="…"` locator among the candidate selectors. That is what
makes a recording reviewable for wording and spelling rather than only replayable.

### Capture checks — assert mode

The **◎ Capture checks** button next to *Start recording* is Playwright codegen's
"assert text" tool. Switch it on and clicking an element no longer drives the
application — it records a check on what that element says right now:

| You click | It records |
| :--- | :--- |
| A heading, label, message, cell | `VERIFY_TEXT` with the exact wording |
| A filled input or dropdown | `VERIFY_VALUE` with its current value |
| Anything with no text | `VERIFY_VISIBLE` |

So a heading reading `Custmoer Master` is captured verbatim, and the day someone
fixes the spelling the run **fails** — `'#t' does not contain 'Custmoer Master'`.
A wording change becomes a test result instead of something a human has to notice.

Switch it off and clicks drive the application again. The toggle survives
navigation, which matters in a single-page application.

**Credentials are never captured this way.** A password field degrades to
`VERIFY_VISIBLE` rather than storing the secret as an expected value, and
`text_full` / `accessible_name` are blanked on any sensitive field — otherwise
they would carry a second copy of the literal straight past the scrubber and into
`actions.json`, which is committed.

`tests/e2e_recorder.py` runs the recorder in a real browser and asserts all of
the above, including that an assert-mode click does not reach the application.

## Step vocabulary

Actions: `NAVIGATE` · `CLICK` · `DOUBLE_CLICK` · `RIGHT_CLICK` · `TYPE` · `CLEAR` ·
`SELECT` · `CHECK` / `UNCHECK` · `KEYPRESS` · `UPLOAD` · `HOVER` · `SCROLL_TO` ·
`WAIT` · `DIALOG` (`accept` / `dismiss` for native `confirm()` boxes) · `SCREENSHOT` ·
`STORE` (capture a runtime value into a variable) · `WAIT_FOR_STATE` (poll until a
field reaches a value, with a deadline — for approval queues and background jobs) ·
`API_REQUEST` (reuses the browser session, so no separate token plumbing) ·
`SWITCH_ROLE` (creator, approver and scanner are different people)

Assertions: `VERIFY_MESSAGE` · `VERIFY_ROW_CONTAINS` · `VERIFY_ROW_ABSENT` ·
`VERIFY_VALUE` · `VERIFY_TEXT` · `VERIFY_DISABLED` · `VERIFY_ENABLED` ·
`VERIFY_READONLY` · `VERIFY_NON_ZERO` · `VERIFY_VISIBLE` · `VERIFY_HIDDEN` ·
`VERIFY_URL` · `VERIFY_COUNT` · `VERIFY_DOWNLOAD` · `VERIFY_OPTION_PRESENT` ·
`VERIFY_OPTION_ABSENT` (dropdown membership — native and PrimeNG/Material/Ant) ·
`VERIFY_API`

Natural-language steps still work (`CLICK ON #save`, `TYPE ON #name with value "Acme"`,
`VERIFY ON table row contains 'Acme'`), so old suites keep running unchanged.

`{{token}}` placeholders are substituted at runtime from the variable pool
(prerequisite data, API-seed captures, CSV data-matrix columns, `last_typed_value`,
`run_token`).

---

## Reliability features

| Feature | What it does |
| :--- | :--- |
| Candidate selectors | Recordings store an ordered list (`data-testid` → stable id → `formcontrolname` → `name` → `aria-label` → label text → structural path), so PrimeNG/Angular ids like `pn_id_8` no longer break replays. |
| Retry + backoff | 2 retries with 2s/4s backoff on every interaction. |
| Auto-heal | Clears toasts/backdrops, retries candidate locators, then optionally Gemini Vision coordinates — **never** on Delete/Submit/Confirm targets. |
| Dialog interception | Native `confirm()` is accepted or dismissed on purpose; a "cancel delete" test finally means something. |
| Download capture | Exports are saved and row-counted (CSV always, XLSX with `openpyxl`) for `VERIFY_DOWNLOAD`. |
| Pagination-aware lookup | Row verification searches the grid's filter box and walks pages instead of only checking page 1. |
| Crash isolation | A dead browser marks one scenario CRASH and the suite carries on. |
| Parallel workers | 1–5 concurrent contexts (forced to 1 in headed mode). |
| Stop button | Cancels a run server-side; a partial report is still written. |
| Pre-flight validation | Checks every generated selector against the live page, and can AI-repair the broken ones. |
| Evidence | Screenshots, per-scenario video, optional Playwright trace, on-screen messages, dialogs, downloads, per-step timings. |

---

## Endpoints worth knowing

| Endpoint | Purpose |
| :--- | :--- |
| `GET /health`, `GET /platform-info` | Status, model, token usage, DB stats. |
| `GET /run-matrix-stream` | Dashboard execution stream (SSE). |
| `POST /trigger-run` → `GET /run-status/{id}` | CI/CD: returns `run_id`, poll for `exit_code`. |
| `GET /export-junit` | JUnit XML for Jenkins / GitHub Actions / Azure DevOps. |
| `GET /export-report-csv`, `/export-report-html`, `/download-evidence` | CSV, printable PDF-ready HTML, evidence ZIP. |
| `GET /compare-runs` | Which scenarios flipped between two runs. |
| `GET /flaky-tests` | Scenarios whose status oscillates. |
| `POST /seed-prerequisite-api` | Pillar 3 — really issues the seeding requests and returns captured variables. |
| `POST /save-schedule` | Nightly / interval runs with webhook notification. |

CI example:

```bash
RUN=$(curl -s -X POST "http://host:8000/trigger-run?project_name=Ambica&flow_name=customer%20master&workers=3" | jq -r .run_id)
until [ "$(curl -s http://host:8000/run-status/$RUN | jq -r .status)" != "running" ]; do sleep 10; done
curl -s "http://host:8000/export-junit?project_name=Ambica&flow_name=customer%20master" -o junit.xml
```

---

## Layout

```
app/                        backend package
├── main.py                 FastAPI app: HTTP surface, recording session, AI orchestration
├── executor.py             Playwright engine: steps, assertions, evidence, parallelism, reports
├── verdict.py              Expectation classification + rule/AI adjudication
├── ai_agents.py            Builder / Breaker / Auditor prompts + step vocabulary
├── llm.py                  Gemini wrapper: retry, usage log, prompt trimming, both SDKs
├── heuristics.py           Idle waits, variable substitution, auto-heal, vision safety
├── recorder.py             Injected JS recorder (multi-candidate selectors, wide coverage)
├── api_seed.py             Pillar 3 API/DB pre-seeding
├── scheduler.py            In-process scheduler + webhook notifier
├── db.py                   SQLite history: runs, results, flaky detection, audit log
└── config.py               Workspace paths, env config, logging

web/                        the interface
├── dashboard.html          shell: sidebar, workflow rail, four views
├── styles.css              design system — type/space scales, semantic status
│                           tokens, light + dark, focus states, motion
├── app.js                  behaviour, kept identical to the original file
├── ui.js                   presentation layer: sidebar bridge, rail, palette,
│                           theme, redesigned renderers (wraps app.js, never edits it)
└── legacy/                 the pre-redesign single-file UI, served at /legacy
projects/                   TEST DATA — <project>/flows/<flow>/{flow,actions,test_cases,report}.json …
tests/                      Unit tests, sample app, E2E, UI smoke, API integration, regression
docs/                       Guides, architecture notes, design blueprint, audit, roadmap
samples/                    Importable example test suite
qa_agent.py                 Shim so `uvicorn qa_agent:app` keeps working
run_server.bat              Windows launcher (frees port 8000, then starts uvicorn)
Dockerfile, docker-compose.yml, requirements.txt, .env.example

logs/, .tmp/, .trash/, qa_platform.db     created at runtime, all gitignored
```

### The interface

Navigation follows the actual workflow — **Record → Generate → Run & verify →
Report** — as a rail across the top, with projects and flows in a persistent
sidebar. A flow's last result shows as a coloured dot next to its name.

* **Verdicts are the design centre.** Each scenario row carries a status glyph,
  the verdict reason in plain language, and — when a scenario comes back
  UNVERIFIED — an **Add assertion** button that opens the editor, because that
  status is an instruction, not just a warning.
* `Ctrl/⌘ K` opens a command palette over flows and every action; `1`–`5` jump
  between views; `Esc` closes any overlay.
* Light and dark both ship, following your system preference with a manual
  toggle in the top bar.
* The previous UI stays available at `/legacy` if you need it.

Both entry points serve the same application:

```bash
python -m uvicorn qa_agent:app    # unchanged, what run_server.bat uses
python -m uvicorn app.main:app    # explicit package path
```

**`projects/` is your data.** It holds every recording, generated suite and
report. Deletes from the UI are soft — they move to `.trash/` — but keep it in
version control or backed up regardless.

---

## Tests

```bash
.venv\Scripts\python.exe -m pytest tests -q           # unit tests
.venv\Scripts\python.exe tests\e2e_selftest.py        # verdict engine vs the sample app
.venv\Scripts\python.exe tests\e2e_chain.py           # dependency chain, rules, polling
.venv\Scripts\python.exe tests\smoke_ui.py            # boots the server, drives the dashboard
.venv\Scripts\python.exe tests\integration_api.py     # CI endpoints, auth, cancellation
```

Two of these carry the weight:

* `tests/e2e_selftest.py` — 13 scenarios asserting each reaches its intended
  verdict, including a boundary test the sample app silently accepts (must be
  FAIL) and an XSS payload that really executes (must be FAIL).
* `tests/e2e_chain.py` — 8 scenarios against `selftest_app/chain.html`, proving
  the chain features actually work: a master's value captured and reused
  downstream, a generated order code flowing between modules, an approval that
  arrives four seconds later being polled for rather than slept through,
  referential integrity, and — the decisive one — **a rule failing a scenario
  that passed on its own terms**.

---

## Notes & limits

* One recording session at a time (`active_session` is global).
* Credentials live in `projects/<project>/config.json` (or legacy
  `login_data.json`) in plain text — protect the workspace folder.
* Vision healing needs an API key and is skipped for destructive controls.
* Visual regression needs `pillow`; XLSX row counts need `openpyxl`.
* Deletes are soft: flows and projects move to `.trash/` with a timestamp.
