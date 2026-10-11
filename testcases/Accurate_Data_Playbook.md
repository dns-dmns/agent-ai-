# Accurate Data Playbook

A step-by-step method for collecting **accurate** module data before writing test
cases — so every sheet uploads without held-back rows and judges PASS/FAIL instead
of UNVERIFIED. Follow the seven steps in order for each module, filling the
companion sheet `Module_Data_Worksheet.csv` as you go.

**The principle:** never type from memory. Every value in the worksheet is
**copied** from one of three sources of truth — the platform's own panel, the
live application screen, or the developer/spec. If you typed it from memory,
it is a guess.

---

## Step 1 — Load the module and open "What's in this module?"

Generate tab → pick the project and flow → click **What's in this module?**

Copy into the worksheet (FIELD rows):

- every **field label exactly as shown** — do not re-type, copy-paste
- its **type** (text / password / dropdown / checkbox / number / date)
- whether it is **required**
- for dropdowns: **every option text**

This panel is the platform's own inventory — it is what your column headers are
matched against, so it is the only correct source for header names.

## Step 2 — Download the template CSV

Click **Download template (CSV)**. Paste its header row into the worksheet
(MODULE row 3). This is the ground truth: if a label in Step 1 and the template
header ever differ, **the template header wins** — that is what the upload
matcher uses.

## Step 3 — Harvest the exact messages from the live app

Open the application and deliberately trigger each message, then **copy the text
from the screen** (select and copy, or from a screenshot) into the worksheet
(MESSAGE rows):

1. Submit the form with a required field empty → copy the required-field message
2. Enter wrong/invalid values → copy the rejection message
3. Complete one valid submit → copy the success message
4. Try to save a duplicate (if applicable) → copy the duplicate message

Ten minutes of this removes almost every UNVERIFIED result, because the Expected
Result sentences can then quote the app's real wording.

## Step 4 — Confirm the rules with the developer or spec

Ask — do not assume (RULES rows):

- Are leading/trailing spaces trimmed?
- Is the key field (username/name/code) case-sensitive?
- Min/max length of each restricted field?
- What happens after N failed attempts (lockout? CAPTCHA? nothing)?
- Any uniqueness or combination rules?

If nobody can answer, probe the live app: try 1 character, try 100 characters,
try the same name twice — and write down what actually happened. Observed
behaviour beats assumption; mark it "observed, not specified".

## Step 5 — Create dedicated, stable test data

Set up and record in the worksheet (DATA rows):

- one **valid record/account** that works today (verify by logging in / saving now)
- one **blocked/inactive** record, if that state exists
- one **existing record** to use for duplicate tests

Rules for this data: it belongs to testing only (never a real user's account),
it is not changed casually, and if a password or value must rotate, the worksheet
is updated the same day. Stale test data is the #1 source of false FAILs.

## Step 6 — Hand over the worksheet, get the sheet back

Give the completed worksheet (or its contents) to Claude. What comes back is the
upload-ready CSV in the platform's template:

`Scenario, Type, <real field columns>, Expected Result`

- `(blank)` = deliberately clear the field; empty cell = leave it untouched
- Expected Results quote the Step 3 messages verbatim
- Scenario names are stable, so later re-uploads update in place

## Step 7 — Upload, run once, and close the loop

1. Upload the sheet. If any row is **held back**, the panel names the row and
   reason — it is almost always a header/field mismatch: fix the header from the
   Step 2 template row and re-upload.
2. Run the suite once. For any **UNVERIFIED** result, look at the run's evidence
   (the messages the platform collected), copy the real wording into the
   worksheet, update the Expected Result, and re-upload — same Scenario name, so
   it updates in place.
3. After the first clean run, the worksheet is the module's data dictionary.
   When the app changes (new field, reworded message), update the worksheet
   first, then the sheet.

---

## Quick reference — where each thing comes from

| You need | Accurate source | Never from |
| :--- | :--- | :--- |
| Column headers | Downloaded template CSV header row | memory / retyping |
| Field types, required, options | "What's in this module?" panel | guessing from the UI |
| Message wording | Copied from the live screen (Step 3) | paraphrasing |
| Validation rules | Developer / spec, else observed probing | assumption |
| Test accounts/values | Dedicated test data, verified today | production users |

**Done means:** every worksheet cell filled from a real source → sheet generated
→ uploaded with zero held-back rows → one run with zero UNVERIFIED.
