# Master prompt — map an application and build its test cases

**How to use:** copy everything inside the box below into a new Claude Code chat
opened in this repository. Replace the three values at the top first. Everything
else stays the same for every project.

---

```text
PROJECT_NAME = Liberty
LOGIN_URL    = https://gc-dev.centralindia.cloudapp.azure.com/liberty/auth/login
SCOPE        = all            (or a menu group, e.g. "master", "master / product catalogue")

You are a senior SDET working in this repository (the QA Automation Platform).
Read CLAUDE.md first; it explains the platform. Then map the application above
and build its test cases, following these steps in order. Report briefly after
each step, and stop to ask me whenever a rule below says "ask".

STEP 1 — Workspace and login
- Make sure projects/<PROJECT_NAME>/ exists (create the workspace if not).
- The explorer logs in with the credentials stored for the workspace
  (projects/<PROJECT_NAME>/login_data.json, gitignored). If none are stored, ask me
  for the username and password, and store them ONLY through
  app.credentials.store(<PROJECT_NAME>, username=..., password=...).
  Never write a password into any other file, test case, log or message.
- Test runs log in through a flow named "Login" with a case named "Valid login".
  If the project has none, create flows/Login/ (flow.json with LOGIN_URL as
  target_url) and a "Valid login" test case that types {{username}} and
  {{password}} into the real login fields, clicks the real login button and
  verifies the page after login. Use the real selectors from the login page.
- Confirm the login works before going further.

STEP 2 — Explore the application (read-only)
- Run:
  .venv\Scripts\python.exe -m app.explorer --project <PROJECT_NAME> --url <LOGIN_URL> --workers 3
  Add --only "<SCOPE>" when SCOPE is not "all".
- It writes, per menu screen, flows/<Menu>/<Screen>/flow.json + actions.json in
  the recorder's own format, and projects/<PROJECT_NAME>/explorer/summary.{json,csv}.
- If screens error: read the log, fix the cause (do not hide it), and finish with
  --skip-existing. The goal is 0 errors.

STEP 3 — Review the recordings like a senior SDET
For every screen with a form, check its actions.json and the summary, and fix
or report anything that would make tests flaky or wrong:
- every field found by a unique selector (formcontrolname / id / label), never a
  placeholder shared by several fields;
- dropdowns opened before an option is chosen;
- no checks on data that changes (page numbers, counts, today's records);
- no generated ids (#pn_id_..., mat-input-...) as the main selector;
- required fields and the real validation messages captured;
- notes in the summary: unexpected_save (STOP and tell me), Save disabled until
  valid, transaction forms not submitted, very large forms truncated.
Give me a table: module | fields | mandatory | validation messages | issues found.

STEP 4 — Write the test cases (Claude source)
For the modules in SCOPE, write test_cases.json in each flow folder, grounded only
in that flow's actions.json, flow.json and the summary — never invent a field, a
selector or a message wording. Merge with
app.sources.replace_source(existing, new_cases, "claude") so cases from other
sources are kept. For each module with a form, cover at least:
- the form opens with its mandatory fields marked;
- auto-generated / read-only fields are read-only;
- each dropdown offers its real options;
- save with every field empty is rejected (quote the real messages);
- one case per mandatory field left empty (quote that field's real message);
- wrong data types refused (letters in number fields, invalid email/phone);
- markup/script in a text field is stored as plain text (security);
- Cancel closes the form without saving;
- search with no match shows the app's real empty-state text;
- list columns and the Add / Edit / Delete controls are present;
- create -> edit -> cancel-delete -> delete, self-cleaning (Master screens only,
  see the data rules below).
Every case: scenario_name, type, expected_intent (success | error | persistence |
sanitised …), steps ending in VERIFY_* checks, expected_result in plain words,
"_generated": "claude". Start each case with NAVIGATE to the screen URL; the
platform logs in by itself (auth flow "Login" / "Valid login").

STEP 5 — Run and fix my own mistakes
- Run each module through the platform's engine (app.executor.execute_suite with
  sources="claude", auto_complete=False, speed "normal", 3 screens).
- For every FAIL / UNVERIFIED / CRASH, open the evidence (reason, screenshot) and
  decide honestly: a bug in my test case -> fix it and re-run; a real
  application defect -> keep the test failing and report it. Never weaken a
  check just to make it pass. Verify on the live page when unsure.

STEP 6 — Final report
Give me:
- per module: cases written, pass / fail, and why each failure fails;
- application defects found (screen, steps, expected vs actual, severity);
- what is NOT covered and why (transactions, approvals, uploads, role-specific
  screens);
- where the files are. Do not commit project data unless I ask.

DATA RULES (always)
- Read-only by default. Never click Delete, Approve, Reject, Submit, Dispatch,
  Post, Issue, Receive or Confirm for real, except a test's own QA-AUTO record.
- Records may be CREATED only on Master screens, only in a dev/test environment,
  named "QA-AUTO <run id>", and each test deletes what it created. Ask me before
  the first run that creates data.
- Transaction screens (orders, purchase, GRN, dispatch, stock, production, job
  work, approvals): document the structure only. Ask me before saving anything
  there, and for the correct order and test data.
- Never overwrite a recording a person made (flow.json without "source":
  "explorer").
- Never type into password, OTP or captcha fields of the application's forms.
- If an empty Save is ever accepted (unexpected_save), stop and tell me — the
  application now has a blank record.
```

---

## What each step produces

| Step | Output |
|---|---|
| 1 | Login stored privately in `projects/<PROJECT>/login_data.json` |
| 2 | `projects/<PROJECT>/flows/<Menu>/<Screen>/flow.json` + `actions.json` for every screen; `projects/<PROJECT>/explorer/summary.csv` (open in Excel) |
| 3 | A review table of every form, with fixes made to any flaky recording |
| 4 | `test_cases.json` per module, labelled **Claude** in the dashboard |
| 5 | Run reports, screenshots and videos per module |
| 6 | A plain-language report: results, real defects, what is not covered |

## Short follow-ups you can send in the same chat

- `Continue with SCOPE = "sales connect"` — map and test another menu group.
- `Re-explore only new screens` — runs the explorer with `--skip-existing`.
- `Allow create/edit/delete on Master screens` — enables the self-cleaning cases.
- `Write test cases for <Menu>/<Screen> only` — one module.
- `Show me the defects found so far` — just the defect list.

## Requirements

- The platform installed (see `USER_GUIDE.md`, section 1).
- A dev or test environment of the application — never production.
- A login that can see the screens you want mapped; screens only other roles can
  see need those roles' logins.





need to change this 
PROJECT_NAME = Liberty
LOGIN_URL    = https://gc-dev.centralindia.cloudapp.azure.com/liberty/auth/login
SCOPE        = all      (or "master", "master / product catalogue", …)
