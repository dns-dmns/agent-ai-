# User Guide — QA Automation Platform

This guide takes you from nothing to a tested module. You record the module once,
get test cases for it, run them, and read the results. No coding is needed.

For *how* the platform works inside, and what it does versus what you do, read
[HOW_IT_WORKS.md](HOW_IT_WORKS.md).

---

## Contents

1. [Before you start (one-time setup)](#1-before-you-start-one-time-setup)
2. [Start the platform](#2-start-the-platform)
3. [Open or create a workspace](#3-open-or-create-a-workspace)
4. [Record the login flow first](#4-record-the-login-flow-first)
5. [Record a module](#5-record-a-module)
6. [Get test cases (five ways)](#6-get-test-cases-five-ways)
7. [Run the tests](#7-run-the-tests)
8. [Read the results](#8-read-the-results)
9. [Re-test your bugs (Defect log)](#9-re-test-your-bugs-defect-log)
10. [Download the automation code](#10-download-the-automation-code)
11. [Other tools in the left menu](#11-other-tools-in-the-left-menu)
12. [Troubleshooting](#12-troubleshooting)
13. [Rules to follow](#13-rules-to-follow)

---

## 1. Before you start (one-time setup)

You need Python 3.12 or newer and a Windows, macOS or Linux machine. Open
PowerShell in the project folder and run:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
copy .env.example .env
```

**Optional — Gemini AI generation.** Open `.env` and set `GEMINI_API_KEY=` to
your key. Without a key everything works except the **Multi-agent generate**
button. The free Gemini tier allows about 20 requests per day.

---

## 2. Start the platform

```powershell
.\run_server.ps1
```

or, with Python directly:

```powershell
.venv\Scripts\python.exe -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** in your browser. Keep the PowerShell window open
while you work; press `Ctrl+C` in it to stop the platform.

> **Port already in use (WinError 10048)?** Another copy is still running.
> `.\run_server.ps1` frees the port for you, or use another port:
> `.\run_server.ps1 -Port 8080`.

---

## 3. Open or create a workspace

A **workspace** is one application you test (for example *Liberty*).

- **Open workspace** — pick it from the list.
- **Create a workspace** — type a name and click **Create**.

The left side then shows:

| Section | What it holds |
|---|---|
| **AUTHENTICATION** | Your login flow(s) |
| **BUSINESS** | Your module flows, in folders like the application's menu (Master → Item Master, RM Item Master …). Click a folder to fold or unfold it. |
| **Tools ▸** (bottom) | Rules, Dependencies, Export project, Defect log, Coverage, Run history, Flaky scenarios, Schedules, Settings. Folded by default so the flow list has room — click **Tools** to open or close it. |

---

## 4. Record the login flow first

Every other module needs to log in, so record login once.

1. Click the **1 Record** tab.
2. **Flow name:** `Login`
3. **Start URL:** your login page, e.g.
   `https://gc-dev.centralindia.cloudapp.azure.com/liberty/auth/login`
4. Click **Launch browser**, then **Start recording**.
5. In the browser that opens: type the username, the password, click **Login**.
6. Click **Stop & save**.

Your password is **not** saved in the test files. It is replaced with
`{{password}}` and the real value is kept in a private file
(`projects/<workspace>/login_data.json`) that is never committed to git.
You can also enter or change the username and password in **Settings**.

---

## 5. Record a module

1. **1 Record** tab.
2. **Flow name:** the module. Use `/` to put it in a folder that matches your
   application's menu — `Master/Item Master`, `Master/RM Item Master`,
   `Business Partners/Customer Master`. In the sidebar they appear as
   **Master → Item Master, RM Item Master**.
3. **Start URL:** the module's list page (recommended), e.g.
   `.../liberty/pages/master/product-catalogue/item-master/list`.
   The platform logs in automatically when it lands on the login page.
4. **Launch browser** → **Start recording**.
5. Use the module the way a tester would: click **Add**, fill the form, **Save**,
   search for the record, **Edit**, **Save**, and (optional) **Delete**.
6. **Capture checks** (optional): click this, then click any text on screen —
   for example *"Item Added Successfully"* — to record a check that this text
   must appear. The button shows **Capturing checks** while it is on; click it
   again to go back to normal recording.
7. **Stop & save**. The module appears under **BUSINESS** on the left.

**Organising existing flows into folders.** Either rename a flow to include the
folder (`item` → `Master/Item Master`), or move its folder on disk into a
sub-folder of `projects/<workspace>/flows/`. The sidebar always mirrors the
folders on disk.

**Tips for a good recording**

- Type plain values (`120`, not `₹ 120` or `1,200`).
- Wait for each screen to load before clicking.
- Clean up: if you created a record, delete it at the end. Records left behind
  stay in the application.

---

## 5b. Map the whole application automatically (explorer)

Instead of recording every module by hand, the **explorer** can visit every
screen in the application's menu and write a recording for each one — the same
`flow.json` + `actions.json` files the recorder makes.

```powershell
.venv\Scripts\python.exe -m app.explorer --project Liberty --url https://.../liberty/auth/login
```

| Option | Meaning |
|---|---|
| `--only "master"` | Only screens whose menu path starts with this (e.g. `"master / product catalogue"`) |
| `--workers 3` | Screens explored at the same time |
| `--headed` | Show the browser while it works |
| `--no-empty-save` | Never click Save, not even on an empty form |

It uses the login stored for the workspace (record the Login flow first).

**What it records per screen:** the page title and breadcrumb, table columns,
buttons, every form label (with its `*`), the real "is required" messages, every
dropdown's options, and sample values typed into every field — then **Cancel**.
Each screen lands in a folder matching the menu, e.g.
`flows/Master/Product Catalogue/Item Master/`.

**What it never does:** save a filled form, click Delete / Approve / Submit /
Dispatch for real, type into password / OTP fields, upload files, or overwrite a
recording you made yourself. On **Master** screens it clicks Save once on the
*empty* form to capture the required-field messages; validation blocks that save.
If a screen ever accepts it, the summary flags it as `unexpected_save`.

**Summary of the whole application:** `projects/<workspace>/explorer/summary.csv`
(open in Excel — one row per field: module, field, type, mandatory, options,
validation messages) and `summary.json`.

Then ask Claude in chat to generate test cases for any of these modules, exactly
as for a module you recorded.

## 6. Get test cases (five ways)

You can mix all of them in one module. Each test case gets a coloured **source
badge** so you always know where it came from, and **creating cases one way
never deletes cases made another way**.

| Way | Badge | Needs |
|---|---|---|
| A. Ask Claude in the chat | **Claude** | Nothing — just a recorded flow |
| B. Gemini multi-agent | **Gemini (API)** | A Gemini API key |
| C. Excel / CSV template | **Excel / CSV** | Filling a spreadsheet |
| D. Data worksheet | **Excel / CSV** | Filling the worksheet |
| E. Coverage enumeration | **Coverage** | Nothing |

### A. Ask Claude in the chat (no API, recommended)

1. Record the flow (step 5).
2. In your Claude chat, write for example:
   *"I recorded the `item` flow in the Liberty workspace — generate all the test
   scenarios."*
3. Claude reads the two files of that flow —
   `projects/<workspace>/flows/<flow>/actions.json` (what you did) and
   `flow.json` (where it starts) — and writes the test cases into
   `projects/<workspace>/flows/<flow>/test_cases.json`, tagged **Claude**.
4. Refresh the dashboard. The cases appear in **3 Run & verify** with a
   **Claude** badge.

Asking again replaces only Claude's earlier cases; Gemini, Excel and coverage
cases are kept.

### B. Gemini multi-agent generation

**2 Generate** tab → pick the flow → choose **Test-type focus** (Positive,
Negative, Boundary, Security…) → **Multi-agent generate**. Three AI agents write
and review the cases. Needs `GEMINI_API_KEY` in `.env`.

### C. Excel / CSV template — write your own cases without selectors

**2 Generate** tab → **Write your own test cases**:

1. **What's in this module?** — shows the fields the platform knows.
2. **Download template (CSV)** — one column per field, plus *Scenario*, *Type*
   and *Expected Result*.
3. Fill one row per test case in Excel, for example:

   | Scenario | Type | SKU Description | Unit Price | Expected Result |
   |---|---|---|---|---|
   | Create valid item | Positive | Test item 1 | 120 | Item Added Successfully |
   | Empty description | Negative | (blank) | 120 | SKU Description is required |

   - **Type**: `Positive` (should succeed) or `Negative` (should be refused).
   - `(blank)` means "leave this field empty".
   - **Expected Result**: copy the exact message the application shows.
4. **Upload filled template**. Rows the platform cannot understand are listed
   with the reason — nothing is guessed.

### D. Data worksheet

Same card → **From a data worksheet**. Download the worksheet, fill in the
module's real messages and test data, then **Build cases from worksheet**. The
platform writes the happy path, one empty-field check per mandatory field, and a
duplicate check.

### E. Coverage enumeration

Left menu → **Coverage** shows which standard behaviours the module has no test
for (duplicate check, edit, export, paging…). **Enumerate field cases** creates
edge cases for each field: empty, too long, wrong type, special characters.

---

## 7. Run the tests

Open **3 Run & verify** and set the **Execution profile**:

| Setting | What to choose |
|---|---|
| **Scenarios** | *All matching* |
| **Mode** | *Headed — watch it* to see the browser; *Headless* for speed |
| **Execution engine** | *Platform — evidence & verdicts* |
| **Verdict engine** | *Rules — evidence based* |
| **Auth flow** | **`Login`** — required for every module except Login itself |
| **Auth scenario** | **`Valid login`** (or the name of your successful-login case) |
| **Screens (run side by side)** | How many browsers run at the same time — see below |
| **Test speed** | How long to wait per step — see below |

### Screens — run test cases side by side

Choose **1, 2, 3, 4, 6, 8** or **Custom…** (type any number; the maximum is set by
`QA_MAX_WORKERS` in `.env`, 8 by default).

- The test cases are **shared between the screens**: each screen takes the next
  waiting test case, so all screens stay busy until the list is done.
- **Headed** mode opens one browser window per screen, **tiled across your
  desktop** so you can watch all of them. Each window shows the full desktop
  layout of the application, scaled down to fit — the app never switches to a
  cramped layout.
- **Headless** mode runs the same number of browsers in the background.
- Each extra screen uses roughly 300–500 MB of memory. On a typical laptop 3–4
  screens is the sweet spot.

Example on Liberty `item`: 12 test cases on 3 headed screens finished in 51 s;
6 test cases on 4 screens in 15 s.

Each test's **video** shows the whole page, filling the frame. With several
screens the videos are smaller (each window is a part of your screen) but still
complete.

### Test speed

| Option | What it does | Use it when |
|---|---|---|
| **Fast** | Moves on the moment the page is ready; shortest time limits | Your environment is quick and stable |
| **Normal** (default) | Waits only while something is loading, with room for slower screens | Most of the time |
| **Careful** | Fixed pauses after every step (the old behaviour) | The application is slow or tests fail randomly on Fast/Normal |
| **Custom…** | Type the **maximum seconds to wait per step** (0.5–120). It still moves on as soon as the page is ready | A screen in your app is known to take, say, 10 s to load |

Your Screens and Test speed choices are remembered in this browser.

The platform waits while the page is genuinely busy — a request in progress, a
visible spinner, the screen still changing — and **stops waiting as soon as it is
ready**. On Liberty, 13 test cases took 274 s on Careful and 106 s on Fast with
the same results.

### Choose which test cases to run

Above the list of scenarios:

- **Filter scenarios…** — search by text.
- **All types** — Positive, Negative, Security, … **This is the type filter the
  run uses.** (The *Test-type focus* chips on the Generate tab only steer
  generation; they no longer limit what runs.)
- **All sources** — pick **Claude**, **Gemini (API)**, **Excel / CSV**, … The
  line under it confirms, e.g. *"Run suite will execute only Claude cases."*
- Tick individual scenarios to run just those, or **▶** on one row to run one.

Click **▶ Run suite**. Each scenario shows its result live.

---

## 8. Read the results

Every test case ends with one of four results:

| Result | Meaning | What to do |
|---|---|---|
| **PASS** | The expected result was **proven** on screen | Nothing |
| **FAIL** | The application did something different from what was expected | Read the reason; it is usually a bug in the application |
| **UNVERIFIED** | Every step ran, but nothing on screen proved the result | Add a check (e.g. the exact success message) to the test case |
| **CRASH** | The browser or the step itself broke | Check the screenshot; usually a changed screen or field |

Each result shows **why**, for example:
*"Expected validation surfaced: 'Minimum Stock is required'"*.

Open **4 Report** for the full report: screenshots, videos, the steps, console
errors and a bug list. **Run history** shows earlier runs; **Flaky scenarios**
lists tests that pass and fail on and off.

> A **FAIL** is not always an application bug. If a field was renamed or became
> mandatory, the test needs updating. The failure reason says which step and
> which field.

---

## 9. Re-test your bugs (Defect log)

Left menu → **Defect log**.

1. Prepare your bug sheet (CSV or Excel). It needs at least a **Module** column
   and an **Expected Result** column. It can also have Defect ID, Steps to
   Reproduce, Actual Result, Priority and Status.
2. **Choose File** → **Preview import** (shows what was understood, changes
   nothing) → import.
3. Run the defect flow. Each bug is reported as:
   - **still open** — the bug is still there,
   - **fixed** — it now behaves as expected,
   - **regressed** — it was closed but has come back.

Defect tests are kept separate from your module's own test cases.

---

## 10. Download the automation code

Left menu → **Export project** → **Download .zip**.

You get a standard Playwright project you can run without this platform:
`pages/` (one class per module), `tests/`, `testdata/`, `playwright.config.js`
and a README. Passwords are never included — `credentials.json` is blank and you
fill it locally.

---

## 11. Other tools in the left menu

| Menu | What it is for |
|---|---|
| **Rules** | Business rules in plain English (e.g. "quantity above 1000 must be rejected"), checked on every test |
| **Dependencies** | Which module needs data from which (Sales Order needs a Customer), so modules run in the right order |
| **Coverage** | What the module is still missing tests for |
| **Run history** | Earlier runs and their results |
| **Flaky scenarios** | Tests that pass and fail on and off |
| **Schedules** | Run a suite automatically (daily, weekly…) |
| **Settings** | Environments (dev/test URLs), the login username and password, and a webhook that is notified after runs |

---

## 12. Troubleshooting

| Problem | Cause and fix |
|---|---|
| Page doesn't open at http://127.0.0.1:8000 | The platform isn't running. Start it (step 2). |
| `WinError 10048` | Port in use. Use `.\run_server.ps1` or `-Port 8080`. |
| "No API key" in the top bar | Only Gemini generation is affected. Add `GEMINI_API_KEY` to `.env` and restart, or use Claude / Excel instead. |
| Every test fails at step 1 | Auth flow is not set. In **Run & verify** set **Auth flow = Login** and **Auth scenario = Valid login**. |
| Tests fail randomly | Switch **Test speed** to **Careful**, or Custom with a larger number. |
| Fewer tests ran than the list shows | Check **All types** and **All sources** above the list — only matching cases run. |
| The watched browser looks zoomed in / forms need scrolling | Windows display scaling (e.g. 150%) shrinks the page. Test browsers render at 100% by default; set `QA_BROWSER_SCALE=1.25` in `.env` if you prefer larger text. |
| "The element was not found" | The screen changed (field renamed, moved, new mandatory field). Re-record the module or ask Claude to update the cases. |
| Template has strange column names like "0.00" | The form's number fields have no readable label. Rename the columns to the real labels shown on screen. |
| 403 "Remote access is disabled" | You opened the platform from another computer. Set `QA_API_KEY` in `.env` on the server and enter it in the dashboard. |

---

## 13. Rules to follow

- **Test on a dev/test environment, never production.** Tests create, edit and
  delete records.
- **Never type real passwords into test cases or Excel sheets.** Use the
  recorded login; the platform stores it privately.
- **Name test data clearly** (e.g. `QA-AUTO …`) and let tests delete what they
  create.
- **Do not delete the `projects/` folder** — it is where all your flows, test
  cases and results live. Deletes from the dashboard go to `.trash/` and can be
  recovered.
