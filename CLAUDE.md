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
.venv\Scripts\python.exe -m ruff check .                       # lint (correctness rules)
.venv\Scripts\python.exe -m pytest tests -q                    # unit
.venv\Scripts\python.exe -m pytest tests/test_core.py -q       # one file
.venv\Scripts\python.exe -m pytest tests -q -k verdict         # one pattern
.venv\Scripts\python.exe tests\e2e_selftest.py                 # real browser, 13 verdict cases
.venv\Scripts\python.exe tests\smoke_ui.py                     # boots server, drives dashboard
.venv\Scripts\python.exe tests\integration_api.py              # CI endpoints, auth, cancellation
.venv\Scripts\python.exe tests\regression_existing_data.py     # backward-compat vs real projects/
.venv\Scripts\python.exe tests\e2e_chain.py                    # dependency chain + app quirks
.venv\Scripts\python.exe tests\e2e_recorder.py                 # recorder JS in a real browser

# Records a narrated walkthrough video of the dashboard (needs a running server)
.venv\Scripts\python.exe tests\record_walkthrough.py --project ornage --flow Login
```

The `tests/test_*.py` files are pytest. The others are
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

### Scenarios and their test cases

`app/coverage.py` holds the level the platform was missing. A **scenario** is the
objective plus a steps template using `{{placeholders}}`; a **test case** is one
row of concrete data with its own `expected_result`. A scenario carries a
`data_table`, and `coverage.expand_scenarios()` — called from
`executor.select_scenarios()` — turns it into one runnable case per row.

Three things follow, and they are the reason this exists:

- **Coverage is enumerated, not judged.** `enumerate_field_cases()` derives the
  permutations from the field's own type and constraints: empty, whitespace,
  min/max boundary, over-boundary, wrong type, unicode, SQL/HTML/template
  injection, and for numbers zero, negative, decimal and int32 overflow. Nothing
  depends on a model remembering. Sensitive fields (password/OTP/captcha) are
  never fuzzed — `learned.is_sensitive` skips them, because a locked account
  fails every later scenario for reasons that have nothing to do with them.
- **Results group.** `build_report` adds `scenario_groups`, so nine failures
  against one field read as one finding — "7 of 9 test cases passed" — instead
  of nine lines that look like nine defects.
- **Gaps are named before a run.** `SCENARIO_CHECKLIST` lists the 16 behaviours a
  CRUD module needs whatever its fields are, and `checklist_gaps()` says which a
  suite never touches. This is what would have caught the missing audit-history
  coverage on Circular Machine. The Auditor agent is *told* the computed gaps
  rather than asked to find them.

Matching is whole-word (`_mentions`): a plain substring test let `"log"` match
*login* and *dialog* and reported audit coverage that did not exist — the same
mistake as the `x`/"close" selector bug below.

A suite with no `data_table` anywhere behaves exactly as before, which is what
keeps existing flows working. The global CSV data matrix
(`expand_data_matrix`) now skips scenarios that carry their own table — 9
enumerated values × 6 CSV rows was a cartesian product nobody wanted.

Endpoints: `/coverage-report`, `/preview-coverage-cases`, `/add-coverage-cases`
(appends, de-duplicated by name). UI in `web/ui-coverage.js` — the sidebar
"Coverage" row, its badge, and the grouped results panel.

### Project export — a separate, strictly read-only feature

`app/export_project.py` turns a recorded project into a standalone Playwright
Page Object Model suite, following the structure a real hand-built suite uses:

```
pages/       one class per module; locators in the constructor
tests/       one spec per module; specs only call page-object methods
testdata/    one JSON per module, plus credentials.json (always blank)
playwright.config.js  package.json  README.md  .env.example  .gitignore
```

It **writes nothing** — it reads flow folders and returns `{path: contents}` for
`main.py` to stream as a zip. `integration_api` proves it by hashing the whole
workspace before and after (`exporting changed nothing on disk`). No model is
called, so the same project always generates identical code.

The generator encodes the rules a real suite learns the hard way, and the tests
pin each one:

- A control with more than one recorded selector becomes `a.or(b)` — the fix for
  a duplicate label and for a conditionally rendered field.
- **A plain click must fail loudly.** Only a click whose *next* step asserts the
  resulting state gets the bounded `timeout` + `.catch()` treatment, because
  there the assertion is the real check. Swallowing every click error would let
  a broken step pass — the same "never turn ignorance into a pass" mistake the
  verdict engine avoids.
- Nothing is counted or row-matched before `waitFor({state:'visible'})`;
  `.count()` does not retry the way `expect()` does.
- Typed values come from the test-data file and fall back to the recorded
  literal, so the suite runs unmodified but is data-driven.
- A step the engine handled internally (`VERIFY_COLUMN_NOT_EMPTY`, `SWITCH_ROLE`,
  `API_REQUEST`, `WAIT_FOR_STATE`…) becomes a visible `TODO` naming what the
  platform did. An honest gap beats generated code that silently checks nothing.
- The README's data table computes "changes every run?" from whether a value came
  from a `{{variable}}` — the reference project called stale hand-copied data its
  single biggest recurring failure.

Credentials are never exported: `testdata/credentials.json` is blank and `.env`
is gitignored. Endpoints `/preview-export` and `/download-project`; UI in
`web/ui-export.js`. Verification worth repeating: `node --check` every generated
file, then execute a page object against a stub `@playwright/test` to see the
real call sequence — parsing is not the same as running.

### Defect log — an additional feature, deliberately disconnected

`app/defects.py` imports a defect sheet and re-tests the bugs it records. It is
kept at arm's length from the main flow on purpose:

- Defect scenarios **never** enter a module's `test_cases.json`. Each module's
  defects get their own flow, `<Flow> [defects]`, with a copy of the recording
  metadata (so auth and the target URL behave) and their own `report.json`. The
  real flow folder is only ever read — `integration_api` asserts the module's own
  suite is byte-for-byte unchanged after an import.
- Nothing in `executor.py` or `verdict.py` changes. This module emits ordinary
  scenarios and the existing engine runs them.

Two column meanings carry the feature. **Expected Result** is a human-written
checkable sentence, which is exactly what `verdict.classify_expectation` reads —
so a defect row is usually judged correctly with nothing invented. **Steps to
Reproduce** is prose: field names resolve against the labels already in
`learned.json` (free and exact), and anything that cannot resolve that way is
*reported, not guessed*. A relative value — "an Outer Diameter less than the
Inner Diameter" — yields a suggestion the user accepts via `/save-defect-data`,
because a fabricated number would make a defect look tested when it was not.
An observation step asserts the wording the *expectation* quotes, so "Observe
the field labelled 'Beam Dish'" becomes `VERIFY_TEXT body = "Beam Disk"` rather
than typing the label into the field.

`defects.outcome()` is what makes the report readable, and it lives outside the
verdict engine so PASS/FAIL keeps its meaning: an **open** defect that fails is
`still_open` (not news), one that passes is `fixed` (the repair landed), and a
**closed** defect that fails is `regressed`.

Endpoints: `/preview-defect-import` (writes nothing), `/import-defects`,
`/save-defect-data`, `/rebuild-defect-flows`, `/defect-status`. CSV needs no
extra package; `.xlsx` needs `openpyxl` and says so if it is missing. UI in
`web/ui-defects.js`.

### Gemini quota — the platform was making it worse

The free tier is **20 requests per day per model**. Three things turned that into
far fewer:

- **Retry amplification.** `RETRYABLE_MARKERS` matches `429`/`quota`, and
  `GEMINI_MAX_RETRIES=3`, so an exhausted *daily* cap was retried three more
  times — four requests spent to fail once. `llm.classify_quota_error()` now
  tells `daily` from `per_minute`: a daily cap raises immediately, a per-minute
  one waits for the delay Google actually named (`suggested_retry_delay`).
- **Vision healing was on by default** and fires on every failed step, so
  *running* a suite competed with *generating* one. `QA_VISION_HEALING` now
  defaults to `0`. Note an explicit `.env` entry still wins.
- **No budget awareness.** A multi-agent run is up to 9 requests (3 agents ×
  3 rounds). `llm.budget_status()` refuses when the day is spent and warns when
  the remainder cannot cover the run. `QA_LLM_DAILY_BUDGET=0` disables the
  warning, which is correct once billing is enabled.

`llm.quota_message()` never says "check your API key" — that message sent a user
to check a key that was working perfectly while the real cause was the cap.
`/llm-status` reports key/model/usage/remaining, and `?probe=true` lists the
models the key can reach via the REST metadata endpoint, which costs no
generation quota and distinguishes an invalid key from a spent allowance.

### Generation must be grounded in the recording

A model handed `target_url = /masters/beamer-capacity` and a recording containing
only `#username` and `#password` invented an entire master-data module from those
two words in the URL — 17 scenarios in which **all 27 selectors were fabricated**.
The prompt had even said "AUTHENTICATION workflow, do NOT generate business
transactional test cases"; the URL won.

Two checks now make that impossible, both deterministic and free:

- `assert_recording_can_ground_generation()` runs **before** any API call in both
  `/analyze-flow` and `/generate-multi-agent-stream`. If the recording is
  credentials-only (`recording_grounding().credentials_only`) while the target URL
  is not a login page and no learned field inventory exists, it refuses with a
  message that says how to fix it. A genuine login flow — credentials-only *and* a
  login URL — is still allowed, as is any recording that reached a real form.
- `generation_grounding()` runs **after**, and reports what fraction of the
  generated selectors appear nowhere in the recording. A model may legitimately
  name a Save button nobody recorded, so the threshold is 70%, not zero; above it
  the suite is flagged in the response, in `generation_meta.json`, and in the UI.

`app.js` turns any non-OK `/analyze-flow` response into the generic "AI analysis
failed.", so `web/ui-coverage.js` replaces `triggerAIAnalysis` (using the
`activeProject()`/`activeFlow()` helpers, since `app.js` keeps those in closure
scope) to show the refusal and the grounding warning in full.

**`node --check` every file in `web/`** — `test_platform.py` does. A syntax error
in an adapter file silently disables every function in it: a broken regex literal
in `ui-coverage.js` made the whole Coverage panel dead, and it surfaced only as a
30-second Playwright timeout.

### Recording fidelity — the effect, not just the action

`actions.json` used to record only *what was clicked*. It said nothing about what
happened next, so the generation agents had to invent the expected result and the
runner had to guess where the application renders its messages. The recorder now
watches for ~1.2s after every CLICK/SELECT (`captureEffect` in `recorder.py`) and
attaches an `effect` block — `url_before`/`url_after`, the messages that appeared
with their selectors, ids that appeared and disappeared.
`main.handle_exposed_event` recognises the `__EFFECT__` event and attaches it to
the *previous* action rather than appending a step, and
`main.describe_recorded_effects()` puts it in the Builder/Breaker prompt as
"assert THESE, do not invent wording". Old recordings have no `effect` key and
behave exactly as before.

### Recording fidelity — codegen-grade capture and assert mode

Two additions that make a recording *reviewable*, not just replayable:

- **Every event carries the element.** `describeElement()` in `recorder.py` adds
  `role` (computed ARIA, explicit or implicit), `accessible_name`, `text_full`
  (whole visible text, capped at 600 chars) and `attrs` (title/placeholder/
  aria-label/href/alt/name/type, plus disabled/readonly on inputs). An exact
  `text="…"` locator joins the candidate list for clickable controls — last
  among the text candidates, because it is the most brittle to a wording change,
  which is precisely what an *assertion* should catch and a *click* should not
  depend on.
- **Assert mode** (`window.__qaAssertMode`, toggled by `POST /set-record-mode`)
  is codegen's "assert text" tool. A click is intercepted in the capture phase
  with `stopImmediatePropagation`, so the application never sees it, and a
  `VERIFY_TEXT` / `VERIFY_VALUE` / `VERIFY_VISIBLE` step is recorded carrying the
  element's exact current text. No executor change was needed — `VERIFY_TEXT`
  already does a substring match, so a corrected spelling fails the replay.

**The security trap this had to avoid:** `credentials.scrub_recorded_actions`
cleans the `value` and `text` keys of a TYPE action. `text_full` and
`accessible_name` would have carried a second copy of the same literal past it
and into `actions.json`, which is committed — the exact leak class the audit
found. So the recorder blanks both on any sensitive field, an assertion on a
password degrades to `VERIFY_VISIBLE` rather than storing the secret as an
expected value, and the scrubber now also masks those keys plus `asserted`
step values.

`tests/e2e_recorder.py` is the first test that runs the recorder **in a real
browser**. It has to be: the script depends on `getBoundingClientRect` and
`getComputedStyle`, and its two riskiest behaviours — that assert mode suppresses
the click, and that no password reaches a captured key — are invisible to the
string checks in `test_platform.py`.

### User-authored test cases — the generate tab without the agents

`app/user_cases.py` + three endpoints (`/module-inventory`, `/user-case-template`,
`/upload-user-cases`) + `web/ui-usercases.js` (the "Write your own test cases"
card in Generate). A tester downloads a CSV whose columns are the module's real
field labels (from `_defect_context`: learned profile first, recording as
fallback), fills one row per case with plain values and an expected result, and
uploads. `build_cases()` synthesizes the steps — CLICK the Add control if the
module has one, CLEAR/TYPE/SELECT/CHECK per cell, CLICK submit — and the
existing verdict engine judges the outcome, so the sheet never contains a
selector or a UI step.

Rules that carry the feature, all tested in `test_platform.py` and
`integration_api.py`:

- **Never guess.** A value under a header that matches no field blocks that row
  with the reason (importing the row minus the value would change what the test
  means); a sheet whose columns match nothing is refused with the module's real
  labels listed. Same philosophy as the defect importer, same reason.
- **Upsert by scenario name.** Re-uploading a sheet updates its own rows in
  place; generated/coverage cases are byte-for-byte untouched (integration test
  asserts it). Imported cases carry `_generated: "user"`.
- **No expected result ⇒ blocked**, because an unverifiable scenario can never
  honestly pass.
- **Credentials**: sensitive fields get `{{username}}`/`{{password}}` in the
  template sample values, and `credential_store.scrub_known_literals` runs on
  upload so a typed literal never reaches the committed suite.
- The template's `SAMPLE —` rows are skipped on upload; `(blank)` means CLEAR,
  not the word "(blank)".

### Accurate Data Worksheet — automating the playbook's manual step

`app/worksheet.py` + `/worksheet-template`, `/preview-worksheet`,
`/import-worksheet` + the "From a data worksheet" panel in `web/ui-usercases.js`.
`testcases/Accurate_Data_Playbook.md` is a human method for collecting accurate
module data (fields, real message wording, test data, rules) before writing
cases; its Step 6 — hand the worksheet over, get an upload-ready sheet back — is
what this automates, deterministically and with no AI.

Design, and why each part is the way it is:

- **The worksheet holds DATA, not scenarios.** `parse_worksheet` reads the
  MODULE/FIELD/MESSAGE/DATA/RULES sections (tolerant of the descriptor row and
  blank rows). `generate_rows` then emits the platform's own template shape
  (`Scenario, Type, <labels>, Expected Result`) and hands it to
  `user_cases.build_cases`, so the run-time path is byte-identical to a
  hand-typed sheet — nothing new touches the executor or verdict engine.
- **Grounded in the inventory, never the worksheet's own field text.** Columns
  are the module's known fields (with selectors); a worksheet FIELD row that
  matches no inventory field is reported in `unmatched_fields`, never turned into
  a guessed selector. If the module has no inventory yet, generation refuses with
  instructions — same rule as `/user-case-template`.
- **A bounded, message-driven scenario set.** Only the three families the
  worksheet's MESSAGE rows describe: happy (success message), one empty case per
  required *text-like* field (required message), duplicate (duplicate message +
  the "record for duplicate test" value). Boundary/injection stays Coverage's
  job — inventing it here is the guessing the playbook forbids.
- **A harvested required-message is quoted only for the field it names**
  (`_required_expected`): pasting "Customer Name is required" onto the Credit
  Limit case would read as testing the wrong thing, so that case gets a generic
  sentence; a field-agnostic message ("This field is required") is quoted for
  every field. Verdicts pass either way (intent-based), so this is about honest
  wording, not pass/fail.
- **Nothing collected is silently dropped**: `captured_unused` surfaces an
  invalid-value message, a blocked-account record, and confirmed rules as
  pointers to Coverage or a manual row.
- Merge is `user_cases.merge_cases` (upsert by name), and
  `credential_store.scrub_known_literals` runs on import, so a value typed into
  the worksheet never lands in the committed suite as a literal.

Proven end to end (`prove_worksheet_run` pattern, and `integration_api`): a
filled worksheet → generated cases → the real engine → four correct PASS
verdicts, with the tester having written no steps and no selectors.

### Message detection — four layers

A false FAIL is the most damaging thing this tool can produce: it blames a
working application. Two OrangeHRM scenarios reported "no validation message
appeared" while `Required` was on screen.

1. **Seed list** — `MESSAGE_SCAN_JS`, the framework selectors.
2. **DOM diff** — `DOM_DIFF_INSTALL_JS` installs a `MutationObserver` (via
   `add_init_script`, so it survives navigation) and treats *text that appeared
   after an action* as a message, whatever its CSS. Flag
   `QA_DOM_DIFF_MESSAGES`, default **on**; it only ever adds messages the static
   list could not see. Noise is filtered by ignoring numeric/clock/pagination
   text, requiring visibility, and skipping anything inside a table body.
3. **Learning** — `learned.remember_message_selector()` persists the classes a
   diff-discovered message used, and `scan_messages()` now reads them back.
   This was **write-only** before: the platform recorded where messages appeared
   and then scanned without that knowledge. Layer 2 is what makes it worth
   having — the seed scan can only ever report a selector already in the list.
4. **AI adjudication** — the pre-existing `verdict_mode=ai`.

**Never convert ignorance into a FAIL.** When no message surface has ever been
observed, "nothing appeared" and "something appeared I cannot read" are
indistinguishable, so the verdict is UNVERIFIED with a next action. Behind
`QA_STRICT_MESSAGE_CONFIDENCE`, default **off**, because it moves existing
verdict numbers (some FAILs become UNVERIFIED — the safe direction).

### Page coverage — controls nobody tested

A recording is a *lower bound* on what the application contains, and the platform
used to treat it as the upper bound, so "Forgot your password?" was never tested
and nothing said so. `/page-coverage` loads the real page, enumerates every
interactive control (`CONTROL_INVENTORY_JS`, including `a[href]`), subtracts
everything the recording and the suite mention, and triages the remainder with
`triage_control()` — `notable` (forgot/export/audit/approve…), `ignorable`
(cookie/privacy/copyright…) or `unknown`. No LLM involved.

### Duplicate testing and uniqueness keys

Duplicate tests used to pass without testing anything. To stop replays colliding
the engine appends a token, so the "duplicate" submission was unique too — and
`make_dep_value_unique()` did that even to a `{{variable}}`, which is deliberate
reuse. That was the actual bug; variables are now left alone.

A contract declares `unique_by: ["#custName", "#effectiveDate"]` — what must
match for a duplicate to *be* one, the date included.
`contracts.duplicate_scenarios()` then builds the two-phase pattern: phase 1
creates a fresh original and `STORE`s every key field; phase 2 replays them all
from those variables. It also generates the more interesting inverse — change
**only** the last key field and the save must succeed, proving the key is neither
narrower nor wider than the business rule.

One trap worth knowing: a generated `expected_result` must avoid negative
vocabulary in explanatory asides. "Refusing it would mean…" made
`classify_expectation` read a correct save as expecting an error.

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

### Persistence — every write goes through `app/storage.py`

`projects/` is the database and it has concurrent writers: a run persisting its
report, the scheduler, parallel workers teaching `learned.json`, the dashboard.
`storage.write_json` / `write_text` write a uniquely named temp file and
`os.replace` it in (atomic; retried briefly on Windows, where a reader holding the
file blocks the swap). `storage.update_json` holds a per-file lock across a
read-modify-write — `run_history.json` and `bugs.json` used to lose a run when two
finished together. `learned.py`'s `remember_*` functions and `credentials.store`
take the same lock. **Never `open(path, "w")` or `Path.write_text` under
`projects/`** — `test_storage.py` proves the lock with 8 threads × 25 writes.
Locks are per process; the platform is one server process.

### Run registry

`executor.register_run()` returns a `RunState` kept in a module-level dict, and
mirrors it into SQLite (`run_state` table) on register and on finish, so
`/run-status/{id}` still answers after a restart; runs still `running` at startup
are marked `interrupted`. `execute_suite` is the one place a raising run becomes
`error` — the scheduler never set it, so a scheduled run that threw read
"running" forever. It powers
the Stop button (`/stop-run` sets `cancelled`, which each scenario checks), CI polling
(`/trigger-run` → `/run-status/{id}`), and the scheduler.

### Test-case sources — `app/sources.py`

Every case carries `_generated`: `claude`, `gemini`, `user` (CSV/worksheet),
`coverage`, `defect`, `rule`, `relationship`, `duplicate`; untagged older cases
read as `manual`. Gemini generation (`/analyze-flow`, multi-agent) used to
**overwrite** `test_cases.json`, silently deleting every uploaded, enumerated and
hand-written case; both now call `sources.replace_source(existing, new, "gemini")`,
which swaps only that source's cases. Untagged cases are never treated as
Gemini's — a regeneration must not delete what it cannot prove it wrote.
`RunConfig.sources` / `?sources=claude,gemini` filters strictly: an empty match
runs nothing, unlike the type filter's fall-back to everything.
`web/ui-sources.js` adds the badge, the "Test source" filter, and appends
`&sources=` to the run stream by wrapping `EventSource` (app.js builds that URL
inline). Claude-written cases are merged the same way with source `claude`.

### Typing into masked number widgets

`loc.fill()` pastes a value in one event. PrimeNG `p-inputnumber` (and any
`role=spinbutton` text input) acts only on keystrokes, so a pasted value skipped
its letter filter and the calculations bound to it — Liberty's CBM stayed empty
and letters typed into Minimum Stock were not recorded as refused.
`is_keystroke_widget()` detects these and TYPE re-enters the value with
`press_sequentially`. Native `type=number` inputs are unaffected.

### Authentication

`api_key_guard` in `main.py`. With no `QA_API_KEY`, only loopback clients are
served; any other address gets 403 unless `QA_ALLOW_REMOTE_WITHOUT_KEY=1`. Docker
requests arrive from the bridge, not loopback, so a container needs the key. With a
key, it travels as the `X-API-Key` header (CI) or the `qa_api_key` cookie (the
dashboard), compared in constant time. A `?api_key=` query parameter is refused
outright — URLs end up in access logs. `tests/test_auth.py` pins each case.

### Reading an expected result — `expected_intent` first, wording second

`verdict.classify_expectation()` used to let any error word win, so "With all
required fields filled, the record is saved successfully" was judged as a
*negative* test and a correct save came back FAIL. It now strips negated error
phrases ("no error is shown", "without any validation error", "should not fail")
and preconditions ("all mandatory fields are filled") before matching the error
patterns, and when both sides still match, an explicitly stated outcome beats a
mere vocabulary word. On the 96 distinct expectations in the repo and
`projects/`, exactly one reading changed ("The record saves": none → success).

A scenario may also carry `expected_intent` (`error`, `success`, `absence`,
`persistence`, `sanitised`, `stability`), which wins outright. The CSV and
worksheet importers set it from the row's Type column (`user_cases.intent_from_type`)
— a harvested message like "Data has been stored" has no success vocabulary, so
judged by wording it was always UNVERIFIED. When Type and wording contradict, the
intent is left undeclared and the row is reported in `warnings`, never silently
resolved. `tests/test_expectation_corpus.py` is the phrase table: **add a row
for every misreading found; never loosen one.**

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
- **`.env` beats a popped env var**: `config.py` calls `load_dotenv()`, which fills
  in anything *absent* from the environment. So `env.pop("GEMINI_API_KEY")` did not
  make a suite offline — a developer's `.env` put the key straight back, and the
  four suites that claim "offline, no API key" only ran offline on CI. They now set
  it to `""`, which `load_dotenv(override=False)` leaves alone. The same trap
  applies to any `QA_*` flag: to test a default, run from a directory outside the
  repo. `test_platform.py` does exactly that for `QA_VISION_HEALING`.
- **CI-only failures are usually this**: an assertion that passes because of local
  configuration. `smoke_ui` checked for `"no API key"` while the `ui.js` redesign
  renders `"No API key"` — green locally with a key, red on CI without one.
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

Verified 2026-10-11 on branch `hardening-w`: `ruff check .` clean, 234 unit
tests, `e2e_selftest` (13 scenarios, each at its intended verdict), `e2e_chain`,
`e2e_recorder` 23/23, `smoke_ui`, `integration_api` 69/69 and
`regression_existing_data` 8/8 all pass.

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
- `requirements.txt` lists `google-genai`, but a venv installed before it was added
  is still on the legacy `google-generativeai` SDK; `pip install -r requirements.txt` makes
  `llm.py` switch automatically.
