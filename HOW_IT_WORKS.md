# How the System Works

This page explains what the QA Automation Platform does, what **you** do, and
what the **system** does for you. Read it before your first use; keep the
[User Guide](USER_GUIDE.md) open for the click-by-click steps.

---

## In one sentence

You show the platform a module once; it turns that into test cases, runs them in
a real browser, and reports **PASS** only when the screen proves the expected
result happened.

---

## The five stages

```
   YOU                      SYSTEM                     SYSTEM                SYSTEM                 YOU
 ┌────────┐  actions   ┌──────────────┐  test   ┌─────────────┐ evidence ┌───────────┐ report ┌────────┐
 │ 1      │ ─────────► │ 2 TEST CASES │ ──────► │ 3 RUN       │ ───────► │ 4 JUDGE   │ ─────► │ 5      │
 │ RECORD │            │ Claude/Gemini│  cases  │ real browser│          │ PASS/FAIL │        │ REVIEW │
 └────────┘            │ Excel/Cover. │         └─────────────┘          └───────────┘        └────────┘
                       └──────────────┘
```

| Stage | You do | The system does |
|---|---|---|
| **1. Record** | Use the module once in the browser the platform opens | Saves every click, typed value and dropdown choice, several ways to find each element, and the messages that appeared after each action. Replaces your password with `{{password}}` and stores the real one privately. |
| **2. Test cases** | Ask Claude in chat, click *Multi-agent generate*, or fill an Excel template | Builds the test cases from your recording: happy path, missing mandatory fields, wrong values, security input, edit, delete, search. Labels each with its source. |
| **3. Run** | Choose settings (auth, source, speed, workers) and click **Run suite** | Opens a fresh browser per test, logs in when needed, performs every step, waits only while the page is loading, and collects evidence. |
| **4. Judge** | Nothing | Compares the expected result with the evidence and decides PASS / FAIL / UNVERIFIED / CRASH, with the reason. |
| **5. Review** | Read the results, file real bugs, update tests when the app changed | Keeps reports, screenshots, videos, run history, a bug list, and flags flaky tests. |

---

## Stage 1 — Recording: what is captured

For every action the recorder stores:

- **What** you did — CLICK, TYPE, SELECT, CHECK, UPLOAD, NAVIGATE…
- **Where** — several selectors for the same element (id, label, text,
  position). If one stops working later, the next one is tried.
- **What happened next** — the URL change and any message that appeared
  ("Item Added Successfully", "Category is required"). Test cases use these real
  messages instead of guessing the wording.

The recording is saved in two files per module:

```
projects/<workspace>/flows/<module>/
    flow.json      where the module starts (URL), when it was recorded
    actions.json   every step you performed
```

---

## Stage 2 — Where test cases come from

| Source | Who writes them | How |
|---|---|---|
| **Claude** | Claude, in your chat | You say "generate test cases for flow X". Claude reads `flow.json` and `actions.json` and writes `test_cases.json`. No API key, no cost per request. |
| **Gemini (API)** | Three Gemini agents (Builder, Breaker, Auditor) | *Multi-agent generate* button. Needs a Gemini key; free tier ≈ 20 requests/day. |
| **Excel / CSV** | You | Fill the module's template — field values and the expected message, no technical steps. The system writes the steps. |
| **Coverage** | The system, no AI | Generates edge cases per field: empty, too long, wrong type, special characters, injection text. |
| **Defect log** | Your bug sheet | Each bug becomes a re-test, kept separate from the module's suite. |

All sources live together in one file, `test_cases.json`. Each case carries a
source label, so:

- creating cases one way **never deletes** cases made another way;
- on the Run screen you can choose to run **only Claude**, **only Gemini**,
  **only Excel**, or everything.

A test case looks like this (you never have to write one by hand):

```json
{
  "scenario_name": "Missing SKU Description is rejected",
  "type": "negative",
  "expected_intent": "error",
  "steps": [
    {"action": "CLICK", "selector": "button:has-text(\"Add Item\")"},
    {"action": "CLICK", "selector": ".p-dialog button:has-text(\"Save\")"},
    {"action": "VERIFY_TEXT", "selector": ".p-dialog", "value": "SKU Description is required"}
  ],
  "expected_result": "'SKU Description is required' is shown and the item is not saved",
  "_generated": "claude"
}
```

---

## Stage 3 — Running: what happens in the browser

For each test case the system:

1. Opens a **new, clean browser** (so one test cannot disturb another).
2. Goes to the module's start URL. If it lands on the login page, it runs the
   **Auth flow** (your recorded Login) with the stored username and password.
3. Performs each step. If an element is not found, it tries the other recorded
   selectors, buttons with similar words (Save / Update / Submit), and the open
   dialog first.
4. **Waits only while the page is busy** — a request still loading, a visible
   spinner, or the screen still changing — and moves on the moment it is ready.
   The **Test speed** setting sets how long it may wait:

   | Speed | Behaviour |
   |---|---|
   | Fast | Shortest limits, no extra pauses |
   | Normal | Waits while loading, with room for slower screens (default) |
   | Careful | Fixed pause after every step, for slow or unstable environments |

5. Collects **evidence**: messages on screen, pop-ups, table rows, downloads,
   browser console errors, and the result of every check step.
6. Saves a screenshot (and a video) for review.

With **Parallel workers** set to 2–5, that many browsers run test cases at the
same time.

---

## Stage 4 — Judging: why PASS means something

Many tools mark a test PASS when no step crashed. This platform asks instead:
**"What was supposed to happen, and is there proof that it did?"**

It reads the expected result — or the case's declared intent — and decides what
kind of check it is:

| The test expects… | Proof the system looks for |
|---|---|
| an **error** (negative test) | A validation or error message on screen, and the record not saved |
| a **success** | A success message, the record in the list, the right page |
| something to be **gone** | The record no longer in the list |
| something to **stay** | The record still in the list |
| input to be **handled safely** | No script ran, no console errors |

Then:

| Result | When |
|---|---|
| **PASS** | The proof was found |
| **FAIL** | The opposite was found (e.g. an invalid record was saved) |
| **UNVERIFIED** | All steps ran but nothing on screen proved the result — deliberately **not** a pass |
| **CRASH** | The browser or a step broke |

Your **Rules** (e.g. "quantity above 1000 must be rejected") are checked on
every test and can turn a PASS into a FAIL.

---

## Stage 5 — What is saved

```
projects/<workspace>/
    config.json            environments, settings
    login_data.json        username/password  (private, never in git)
    rules.json             your business rules
    learned.json           what the system learned about the app's fields
    bugs.json              failures from the latest runs
    flows/<module>/
        flow.json          start URL
        actions.json       the recording
        test_cases.json    the test cases (all sources)
        report.json        latest results
        run_history.json   all earlier runs
        screenshots/ videos/ traces/   evidence
```

The **Export project** button turns all of this into a standalone Playwright
project you can run anywhere.

---

## What the system does on its own vs. what needs you

| The system handles | You decide |
|---|---|
| Finding elements, even when one selector breaks | What a module **should** do (the expected results) |
| Logging in before each test | Which business rules matter |
| Waiting for loading screens | Which environment to test (never production) |
| Collecting evidence and screenshots | Whether a FAIL is a bug or an intended change |
| Proving PASS / FAIL with a reason | Fixing tests when a screen is redesigned |
| Keeping passwords out of test files | Keeping test data clean in the application |

---

## Safety built in

- **Passwords** are never written into test cases, reports or exports.
- **Deletes** from the dashboard go to `.trash/` and can be restored.
- **No guessing.** If an Excel column or a defect step cannot be matched to a
  real field, the row is reported back to you instead of being invented.
- **No AI invention.** AI generation is refused if the recording does not show
  the module, and flagged when most of its selectors were made up.
- **Remote access** is off unless an API key is set, so others on the network
  cannot drive your browser.

---

## Limits to know

- The platform tests what it can **see** in the browser. It does not read the
  application's database or source code.
- Each module must be **recorded once** before test cases can be made for it.
- Expected results must be **checkable on screen** — "the record is saved and
  shown in the list", not "the data is correct".
- Tests create real records. Use a dev/test environment and let tests delete
  what they create.
- Parallel runs share one login; modules that depend on each other (Customer
  before Sales Order) should run in order, using **Dependencies**.
