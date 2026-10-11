# Enhancement Plan — recording fidelity, coverage and data flow

**Created:** 2026-08-02
**Updated:** 2026-08-03
**Status:** §1–§3 and §5 implemented and covered by tests. §4c (ordered scenario
chains) and §6 (Surveyor agent) deliberately deferred — see the status table below.
**Source:** issues found while running the platform against the OrangeHRM demo
(`projects/ornage`) and while planning the 10-module workflow in `QA Strat/`

---

## Implementation status

| § | Item | State | Where |
| :-- | :--- | :--- | :--- |
| 1 | Effect capture while recording | **Done** | `recorder.py` `captureEffect()`, attached by `main.handle_exposed_event` |
| 1 | Effects fed into generation | **Done** | `main.describe_recorded_effects()` → the Builder/Breaker prompt |
| 2 | Layer 1 seed selector list | Done (pre-existing) | `executor.MESSAGE_SCAN_JS` |
| 2 | Layer 2 DOM-diff detection | **Done** | `executor.DOM_DIFF_INSTALL_JS`, flag `QA_DOM_DIFF_MESSAGES` (default **on**) |
| 2 | Layer 3 calibration / learning | **Done** | `learned.remember_message_selector()`, now read back by `scan_messages()` |
| 2 | Layer 4 AI adjudication | Done (pre-existing) | `verdict_mode=ai` |
| 2 | "Never FAIL on ignorance" | **Done** | flag `QA_STRICT_MESSAGE_CONFIDENCE` (default **off** — it moves verdict numbers) |
| 3 | Page inventory + uncovered controls | **Done** | `/page-coverage`, `main.CONTROL_INVENTORY_JS`, `triage_control()` |
| 3 | Coverage panel | **Done** | `web/ui-coverage.js` → Coverage → Page coverage |
| 4a | `STORE` + `{{var}}` in one scenario | Done (pre-existing) | proven by `e2e_chain` |
| 4b | Cross-flow contracts | Done (pre-existing) | proven by `e2e_chain` |
| 4c | Ordered scenario chains | **Deferred** | trades away isolation; nothing needs it yet |
| 5a | `{{var}}` never uniquified | **Done** | `executor.make_dep_value_unique()` |
| 5b | `unique_by` declaration | **Done** | `contracts.normalise()` |
| 5c | Two-phase duplicate pattern | **Done** | `contracts.duplicate_scenarios()`, `/preview-duplicate-checks` |
| 6 | Surveyor agent | **Deferred** | needs an API key and can touch data it was not told to touch |
| 7 | Self-test proof for all of the above | **Done** | `chain.html` §8–§9, `e2e_chain`, `integration_api` |

Two things learned while building it, both now guarded by tests:

* `remember_message_selector()` was **write-only** — the platform recorded where
  messages appeared and then scanned without that knowledge. Worse, the seed scan
  could only ever report a selector already in the list, so nothing new was ever
  learned. Layer 2 is what makes layer 3 worth having.
* A generated `expected_result` must avoid negative vocabulary in explanatory
  asides. "Refusing it would mean…" made the intent classifier read a correct
  save as expecting an error, failing a scenario that behaved properly.

---

## Summary

Five problems were found in real use. Four of them share a single root cause:

> **The platform only knows what the recording showed it.**
> The recording is a *lower bound* on what the application contains, and the
> platform currently treats it as the upper bound.

| # | Problem observed | Root cause |
| :-- | :--- | :--- |
| 1 | Two scenarios reported "no validation message appeared" when the message was clearly on screen — a **false FAIL** | Message detection is a static list of framework CSS selectors |
| 2 | "Forgot your password?" on the login page was never tested | Only recorded controls are known to exist |
| 3 | No way to assert the message shown *after* a successful login / password reset / customer creation | The recorder captures the action but not its effect |
| 4 | A customer ID generated in one place cannot easily be used downstream | Three different data-flow cases are being conflated |
| 5 | Duplicate-record tests never actually create a duplicate; the date in the uniqueness key is ignored | Unique-data mode directly contradicts duplicate testing |

A false FAIL is the most damaging outcome this tool can produce. It makes the
tool blame a working application, and trust is lost faster from one false alarm
than from a missed bug. Problems 1 and 5 both produce results that are wrong
rather than merely incomplete, so they rank above the others.

---

## 1. Record the effect, not just the action

**This is the highest-value change. It fixes problems 1 and 3, and improves 2.**

### What happens today

`actions.json` records: *clicked `#username`, typed "Admin", clicked Login.*
It records nothing about what happened next — no success message, no URL change,
no new elements. So the generation agents must **guess** the expected result, and
the runner must **guess** where this application renders its messages.

### What to record instead

Each recorded action gains an `effect` block, captured by a `MutationObserver`
plus a URL comparison inside the existing injected recorder script:

```json
{
  "action": "CLICK",
  "selector": "button:has-text('Save')",
  "effect": {
    "url_before": "/customer/new",
    "url_after":  "/customer/list",
    "messages": [
      { "text": "Customer ACME saved successfully",
        "selector": ".oxd-toast-content-text",
        "kind": "success" }
    ],
    "appeared":    ["#customerCode"],
    "disappeared": ["#saveBtn"]
  }
}
```

### What this buys

| Problem | How the effect block solves it |
| :--- | :--- |
| Messages after login / password reset / customer creation | The **exact** text is captured while recording, so the agent writes `VERIFY_MESSAGE "Customer saved successfully"` rather than inventing wording |
| False FAIL on validation | The message **selector** is learned from the application itself; no static framework list required |
| "It only captures what I click" | A recording becomes a small model of behaviour instead of a click log |
| Weak generated expectations | `expected_result` becomes grounded in observed reality — which is exactly what the verdict engine needs in order to prove anything |

### Known limitation

Recording only the happy path never triggers a validation error, so error
selectors are still not learned from that session. Two cheap remedies:

* record one deliberate failure (submit the form empty once — five seconds), or
* let the runner learn passively: the first time it classifies a message, persist
  that selector to the project config.

**Agent required:** no. This is browser observation.

---

## 2. Message detection — four layers, cheapest first

Replaces the static-selector approach that caused the false FAIL.

| Layer | Mechanism | Cost |
| :--- | :--- | :--- |
| 1. Seed list | The existing `MESSAGE_SCAN_JS` framework selectors (PrimeNG, Material, Ant, Bootstrap, oxd) | free |
| 2. **DOM diff** | A message is *text that appeared after an action and was not there before*. A `MutationObserver` records newly **inserted** nodes with visible text; the existing `classify_message()` sorts them into error / success / unknown | free, framework-agnostic |
| 3. Calibration | Once per flow, deliberately submit an empty form, observe what appears, and persist the discovered selectors to `projects/<p>/config.json → message_selectors` | one page load |
| 4. AI adjudication | The existing `verdict_mode=ai` — screenshot to Gemini. Last resort only | tokens |

Layer 2 is the real fix: it would have found `Required` with zero knowledge of
OrangeHRM. Noise (clocks, counters, table refreshes) is filtered by ignoring
purely numeric or time-like text, capping length, requiring *insertion* rather
than mutation, and ignoring nodes inside a table body being refreshed.

Layer 3 is the "learn it by running scenarios" idea, made explicit and persistent.

### The governing principle

> **Never convert ignorance into a FAIL.**

A negative scenario may only FAIL when the engine can say *with confidence* that
nothing appeared. Confidence means the application's message surface is known
(calibrated, or previously observed) **and** nothing appeared. If the surface has
never been seen, the honest verdict is **UNVERIFIED** — "something may have
appeared that I cannot read; calibrate this flow" — never FAIL.

Applying this rule alone would have turned the two false FAILs into two honest
UNVERIFIEDs, each with a clear next action.

**Agent required:** no (layer 4 uses the AI path that already exists).

---

## 3. Page inventory — controls that were never recorded

Fixes problem 2 ("Forgot your password?").

### Approach

1. **Inventory** — load the flow's target URL and enumerate every interactive
   control: `input`, `button`, `a[href]`, `select`, `textarea`, `[role=button]`,
   `[tabindex]`. Reuses the page-loading machinery already built for the
   selector pre-flight validator.
2. **Diff** against everything the recording touched and everything the generated
   scenarios reference. The remainder are **uncovered controls**.
3. **Triage** — decide which gaps matter. "Forgot your password?" does; a footer
   copyright link does not. This is the one place judgement is needed, and the
   only part that benefits from an LLM.
4. **Feed forward** — pass the triaged list to the Builder and Breaker as explicit
   requirements, and let the Auditor treat an uncovered control as a coverage gap
   (the mechanism it already uses for its 25-point matrix).
5. **Show it** — a "Page coverage" panel in the UI:

```
7 controls found · 3 exercised · 4 never tested
  ✗ a: "Forgot your password?"
  ✗ input: remember-me
  ✗ select: language
  ✗ a: "OrangeHRM, Inc" (footer)
```

Even with no AI at all, that list answers "what did I forget?".

### Boundary

The inventory proves the link **exists and is untested**. It cannot test what is
*behind* it — the password-reset page is a separate screen with its own fields.
Two options:

* **Cheap and safe:** flag it and record that flow yourself (about 30 seconds).
* **Ambitious:** a Surveyor agent explores one level deep (see §6).

**Agent required:** no for steps 1, 2, 5. Small LLM step for triage.

---

## 4. Using a generated ID downstream

Problem 4 has **three different answers**, and conflating them is what makes it
feel difficult.

### a) Later steps in the same scenario — works today

```
TYPE   #custName          "ACME {{run_token}}"
CLICK  #saveBtn
VERIFY_MESSAGE            "saved successfully"
STORE  #customerCode  AS  customer_code        ← captures what the app generated
VERIFY_ROW_CONTAINS table {{customer_code}}
NAVIGATE                  /orders/new
SELECT #customerDropdown  {{customer_code}}
```

`{{customer_code}}` substitutes anywhere afterwards — **including inside a
selector**, e.g. `tr:has-text('{{customer_code}}') button.edit`.

### b) A different flow (customer master → order module) — works today

The provider flow declares a contract:

```json
{ "provides": [{ "entity": "customer",
                 "var": "customer_code",
                 "name_field": "#customerCode" }],
  "run_once": true }
```

Chain it as a prerequisite and the order flow receives `{{customer_code}}`
automatically, seeded **once per run** rather than before every scenario.

### c) Scenario 1 → scenario 5 in the same suite — deliberately unsupported

Each scenario runs in its own browser context. That isolation is what allows
scenarios to run in parallel, to be re-run individually, and to avoid failing in
a domino chain when an early scenario breaks. Sharing state between scenarios
would trade away all three.

**Recommended order of preference:**

1. **Make each scenario self-sufficient** — it creates the data it needs. Slower,
   but any scenario runs alone, in any order, in parallel. This should be the
   default.
2. **Use a prerequisite flow** for data many scenarios share — exactly what
   run-once seeding exists for.
3. **Ordered chains — only when the workflow genuinely is sequential** (the
   M6 → M7 → M8 → M9 → M10 approval workflow, where an order must exist before it
   can be approved). Those scenarios run in sequence, in one context, sharing a
   variable pool, and are excluded from parallel execution. Opt-in and visibly
   marked, because it trades away isolation.

**Agent required:** no.

---

## 5. Duplicate testing and uniqueness keys

Problem 5 is a genuine design conflict, and today it produces tests that pass
without testing anything.

### The conflict

To stop repeat runs colliding with existing records, the platform appends a
unique token (`ACME_183927e89`). A duplicate test needs **exactly the opposite** —
the same value submitted twice. With uniquification active, the second attempt is
also unique, no duplicate ever occurs, and the scenario "passes" having proven
nothing.

Added to that: if the application's uniqueness key is *name + date*, two records
with the same name but different dates are legitimately **not** duplicates. A test
that only repeats the name proves nothing either.

### The fix — three parts

**a) `{{variables}}` are never uniquified.** A value taken from a variable is
deliberate reuse and must be treated as sacred. The absence of this rule is the
actual bug.

**b) Declare the uniqueness key**, alongside the contract:

```json
{ "unique_by": ["#custName", "#effectiveDate"] }
```

Now the engine knows what must match for a duplicate to be a duplicate — the date
included.

**c) A two-phase duplicate pattern** the generator can build correctly:

```
── phase 1: create the original ──
TYPE   #custName        "ACME {{run_token}}"
TYPE   #effectiveDate   "2026-08-02"
CLICK  #saveBtn
VERIFY_MESSAGE          "saved successfully"
STORE  #custName      AS dup_name          ← exact values, token included
STORE  #effectiveDate AS dup_date

── phase 2: repeat every field in the uniqueness key ──
CLICK  #newBtn
TYPE   #custName        "{{dup_name}}"     ← identical on purpose, never re-tokenised
TYPE   #effectiveDate   "{{dup_date}}"     ← the date matters too
CLICK  #saveBtn
VERIFY_MESSAGE          "already exists"
```

Expected result: *a duplicate error appears and no second record is created* —
provable, and still provable on the hundredth run, because phase 1 always creates
a fresh original.

**Bonus:** the same `unique_by` declaration lets the engine generate the negative
case automatically — change **only** the date and assert the save **succeeds**,
proving the uniqueness key is exactly what the business says it is, neither
narrower nor wider.

**Agent required:** no.

---

## 6. Do we need another agent?

One, and only for judgement.

| Capability | Agent? | Why |
| :--- | :--- | :--- |
| Effect capture while recording | **No** | Browser observation |
| DOM-diff message detection | **No** | Deterministic, offline, free |
| Message calibration | **No** | A scripted probe |
| Page inventory | **No** | A DOM query |
| `unique_by`, duplicate pattern, `{{var}}` protection | **No** | Engine rules |
| Ordered scenario chains | **No** | Execution mode |
| Deciding *which* uncovered controls matter | **Small LLM step** | "Forgot password matters, the cookie banner does not" is judgement |
| Exploring *behind* a link (new pages) | **Yes — Surveyor** | Requires navigating, observing, and deciding what is safe |

### Surveyor safety constraints (non-negotiable)

Clicking unknown elements on a live ERP creates and deletes real records.

* **Read-only by default** — inventory the DOM, never click.
* If exploration is enabled: follow **links only**, never buttons.
* Never anything matching the existing `DESTRUCTIVE_KEYWORDS` list.
* Never submit a form.
* Only on an environment explicitly marked safe.

This is the same discipline that already prevents Vision AI from clicking Delete.

---

## 7. Will existing flows break?

| Change | Risk | Detail |
| :--- | :--- | :--- |
| Effect capture in the recorder | ✅ None | Additive field in `actions.json`; old recordings lack it and behave exactly as today |
| Page inventory / coverage panel | ✅ None | Read-only, generation-time only |
| `{{var}}` never uniquified | ✅ None | Today's uniquification of a variable is a bug; this only fixes broken cases |
| `unique_by` + duplicate pattern | ✅ None | New optional contract field; suites without it are unaffected |
| Message calibration | ✅ None | Opt-in; writes to project config |
| Ordered scenario chains | ⚠️ Opt-in | Chained scenarios lose parallelism and independent re-run — must be visibly marked so nobody enables it accidentally |
| DOM-diff messages at run time | ⚠️ Changes verdicts | A positive scenario silently ignoring an on-screen error will now correctly FAIL. A true finding, but numbers move once — ship behind a flag with a note in the report |
| "Never FAIL on ignorance" | ⚠️ Changes verdicts, softer | Some current FAILs become UNVERIFIED. Numbers move once, in the safe direction |

Nothing changes the step vocabulary, the `test_cases.json` schema, or the report
format. Existing suites keep running unchanged.

### How to verify it

Extend the bundled self-test app (`tests/selftest_app/`) with:

* a validation message using a **deliberately unusual CSS class** belonging to no
  known framework — proving DOM-diff finds it without help;
* an interactive control that no recording touches — proving the inventory flags it;
* a record with a **composite uniqueness key** (name + date) — proving the duplicate
  pattern triggers a real duplicate error, and that changing only the date is
  correctly accepted.

---

## 8. Build order

| Step | Work | Needs API key |
| :-- | :--- | :--- |
| 1 | **Effect capture in the recorder** — biggest single win; fixes messages, grounds expectations, learns the application's selectors | No |
| 2 | **`{{var}}` protection + `unique_by` + duplicate pattern** — small, and converts a whole category of fake-passing tests into real ones | No |
| 3 | **Page inventory + coverage panel** — immediately answers "what did I forget?" | No |
| 4 | **DOM-diff messages at run time + "never FAIL on ignorance"** — retires the static selector list permanently | No |
| 5 | **Ordered scenario chains** — only once a genuinely sequential workflow needs them | No |
| 6 | **Surveyor agent** — last, and read-only to begin with | Yes |

Steps 1–3 cover almost everything reported here and need no AI at all.

---

## 9. Open decisions

1. **Should a newly detected error message fail a previously passing positive
   scenario?** Recommended: yes — it is a real defect that was being hidden — but
   expect report numbers to move on the first run after the change.
2. **Independent scenarios or ordered chains as a first-class mode?** Independence
   is the better default (robust, parallel, individually re-runnable). Ordered
   chains are only justified by genuinely sequential workflows such as the
   M6 → M10 approval chain. This choice affects how steps 1 and 5 are built, so it
   is worth settling first.
3. **Exploration behind links at all**, or is flagging *"Forgot password is
   untested — go record it"* sufficient? Flagging is free and completely safe;
   crawling is useful but is the only part of this plan that can touch data it was
   not explicitly told to touch.
