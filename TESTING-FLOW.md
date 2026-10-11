# Mount ERP — Playwright POM Testing Flow

This document describes the complete, module-by-module automated testing flow
built for the Mount ERP application (`gc-dev.centralindia.cloudapp.azure.com/mount-web`),
so the same structure/approach can be reused on the next project.

---

## 1. Project Structure (Page Object Model)

```
pages/        -> one class per screen/module, wraps locators + actions
tests/        -> one spec file per module, orchestrates the page objects
testdata/     -> JSON files with form data + live record numbers
playwright.config.js
```

**Pattern used in every page object:**
- Extends `BasePage` (login, navigation, shared helpers).
- Constructor declares all locators as `this.xyz = page.locator(...)`.
- Methods are the actions a real user would take (`fillX()`, `clickY()`, `selectZ()`).
- Spec files never touch raw locators directly — they only call page object methods.

**`pages/BasePage.js`** — shared by every page object:
- `gotoLogin()` / `login()` — fills username/password, waits for redirect into the app.
- `selectPlant()` — picks the working plant (`TESTING PLANT`) from the header dropdown.
- `expandSidebar()` / `openMenuGroup(label)` — opens the collapsible sidebar sections (CRM, Sale Orders, Planning, Production, Dispatch, Warehouse...).
- `loginAndSelectPlant()` — the one call every spec's `goto()` starts with: login → land on dashboard → pick plant.

---

## 2. The End-to-End Business Flow (module order)

The real business process this suite mirrors, in order:

```
Deal → Quotation → Sale Order Approval (L1 + L2) → Planned Order
  → Production Order → Job Card → Production Floor (scan bundles)
  → Storage Assignment → Dispatch (Token → Approve → Plan → Gate In
  → Pick List → Loading → Documents → Gate Exit → Delivery Confirmation)
```

Each stage below names its spec file, page object(s), and test data file(s).

### 2.1 Login
- **Spec:** `login-page.spec.js`
- **Page object:** `LoginPage.js`
- **Data:** `logindata.json` → `dataloginform` (`username`, `password`)
- Covers: valid login, empty username, empty password.

### 2.2 Deal
- **Spec:** `deal-creation.spec.js`
- **Page object:** `DealPage.js`
- **Data:** `logindata.json` → `dealdataform`
- Fills the full deal form (account/contact/address/billing/shipping) and saves.

### 2.3 Quotation
- **Specs:**
  - `quotation-creation.spec.js` — negative/validation tests (missing deal, missing contact fields, missing billing/shipping, missing GST/PAN, etc.) using `quotationdata.json`.
  - `real-quotation-creation.spec.js` — the full happy-path, end-to-end creation using `realquotationdata.json`, including the **post-creation workflow**:
    `Draft → Send to Customer → Delivered → Confirm → Confirmed → Upload PO → Convert to SO`.
- **Page object:** `QuotationPage.js`
- **Notes:**
  - PO dates are computed dynamically (`Date.now()`-based) instead of hardcoded, so the test never fails just because the calendar moved on.
  - `convertToSO()` waits for the resulting **Sale Order number** to appear rather than trusting the click itself to "settle," because the button disappears from the DOM the instant conversion succeeds (a common Angular SPA race).

### 2.4 Sale Order Approval
- **Spec:** `sales-order-approval.spec.js`
- **Page object:** `SalesOrderApprovalPage.js`
- Approves the first row in **L1 (Sales Approval)**, then **L2 (Finance Approval)** for today's order.
- **Writes:** `approvedSalesOrder.json` (`soNumber`) — *for the next spec to read manually* (not auto-wired).

### 2.5 Planned Orders
- **Spec:** `planned-orders.spec.js`
- **Page object:** `PlannedOrdersPage.js`
- **Reads:** `plannedorderdata.json` → `soNumber` (must be an **approved, not-yet-planned** SO — usually hand-copied from `approvedSalesOrder.json`).
- **Writes:** `createdPlannedOrder.json` (`plannedOrderNumber`).

### 2.6 Production Order → Job Card → Production Floor
- **Spec:** `production-order.spec.js`
- **Page object:** `ProductionOrderPage.js`
- **Reads:** `productionOrderData.json` (`plannedOrderNumber`, `workCenterName`, `scheduledDate`, `productionOrderNumber`).
- Four tests in sequence:
  1. **Convert Order** — looks up the planned order, assigns a work center + scheduled date, creates the Production Order.
  2. **Create Job Card** — searches eligible orders, creates the job card.
  3. **Job Cards** — releases the created job card.
  4. **Production Floor** — scans every bundle for that production order.

### 2.7 Storage Assignment (Warehouse)
- **Spec:** `storage-assignment.spec.js`
- **Page object:** `WarehouseStorageAssignmentPage.js`
- **Reads:** `productionOrderData.json` (`productionOrderNumber`), `storageAssignmentData.json` (`storageLocation`).
- Scans each pending bundle (`<productionOrderNumber>-1`, `-2`, ...) and assigns it to a storage location.

### 2.8 Dispatch (the longest chain — one spec, seven page objects)
- **Spec:** `dispatch-token-creation.spec.js` (single test, deliberately kept as **one** spec file even though it spans many screens — matches the real user's single continuous session).
- **Page objects** (one per screen, split for maintainability, all driven from the one spec):
  1. `DispatchTokenPage.js` — Select Sale Order → Create Token → open Dispatch Tokens list → Approve.
  2. `LogisticsPlanPage.js` — Create Plan → select the approved token → Add Truck. *(Its list search needs an explicit "Search" button click — doesn't auto-filter on typing, unlike most other search bars in this app.)*
  3. `GateEntryPage.js` — search by vehicle number (not by DT token — the search bar doesn't index that), Gate In, confirm.
  4. `PickListPage.js` — Generate Pick List, pick every bundle, Complete. *(List rows show PK/LP/SO numbers, never the DT token — match by the LP/plan number instead.)*
  5. `LoadingPage.js` — scan every bundle (**bundle IDs are production-order IDs, `PR-<root>-<seq>-<n>`, not the DT token ID** — derive by swapping the `DT-` prefix for `PR-`), upload after-loading photo, Mark Truck as Loaded, Complete Dispatch Token.
  6. `DocumentsPage.js` — Add Invoice, Add E-Way Bill, confirm each, Generate Packing List, Confirm Documents.
  7. `GateExitPage.js` — Gate Exit, Confirm Gate Exit.
  8. `DeliveryConfirmationPage.js` — Confirm Delivery (matched by the **LP/plan number** — this screen never shows the DT token number either).
- **Data:** `dispatchTokenData.json` (`soNumber`, `truck` details, `afterLoadingPhotoPath`, `invoiceNumber`, `ewayBillNumber`).
- **Key lesson repeated across this whole module:** almost every downstream screen (Pick List, Gate Entry, Delivery Confirmation) identifies records by the **LP (Logistics Plan) number**, not the DT token number — the token number is only used for the first 2–3 screens.

---

## 3. Test Data Strategy

| File | Type | Changes every run? |
|---|---|---|
| `logindata.json` | credentials + deal form | No (static) |
| `quotationdata.json` | form-fill data for negative tests | No (static) |
| `realquotationdata.json` | full happy-path form data | No (dates auto-computed in code) |
| `plannedorderdata.json` | `soNumber` | **Yes** — must be a currently-approved, not-yet-planned SO |
| `createdPlannedOrder.json` | `plannedOrderNumber` | Auto-written by `planned-orders.spec.js` |
| `productionOrderData.json` | `plannedOrderNumber`, `workCenterName`, `scheduledDate`, `productionOrderNumber` | **Yes** — plannedOrderNumber must be eligible; scheduledDate must stay future |
| `approvedSalesOrder.json` | `soNumber` | Auto-written by `sales-order-approval.spec.js` |
| `storageAssignmentData.json` | `storageLocation` | No (static) |
| `dispatchTokenData.json` | `soNumber`, truck/invoice/e-way details | **Yes** — soNumber must have produced, undispatched items |

**Important nuance:** the "auto-written" files (`approvedSalesOrder.json`, `createdPlannedOrder.json`) are **not automatically read** by the next spec — they're written for a human to copy the value into the next spec's hand-edited input file. This chain is the main source of "stale data" failures — the fix is always to re-copy the latest live record number forward before running the next stage.

---

## 4. Recurring Technical Patterns / Lessons Learned

These are the actual bugs hit and fixed while building this suite — worth carrying into the next project as house rules:

1. **Ambiguous `getByRole` locators** (matching 2+ elements with the same visible text, e.g. a sidebar button and a dashboard button both called "Production") → scope locators as tightly as possible (`button.nav-group-header`, not a bare role+name match).
2. **Click-causes-element-to-disappear races** (Angular re-renders and removes the very button you just clicked, e.g. "Convert to SO", "Pick" buttons) → don't let a single `.click()` retry loop hang for the full test timeout; give it a short bounded timeout, `.catch(() => {})`, then assert on the **resulting state** instead of the click "settling" cleanly.
3. **`.count()` doesn't auto-retry** like `expect()` does — always `waitFor({ state: 'visible' })` on the first expected element before counting/looping, especially right after a navigation or modal open.
4. **IDs shown on one screen ≠ IDs needed on the next.** This app frequently displays a *different* number per screen for the *same* underlying record (DT token vs. LP plan vs. PR production-order vs. PK pick-list) — always confirm which ID a given screen's search/filter actually matches against before wiring a lookup.
5. **Not every search bar auto-filters on typing** — some (Logistics Plans) require an explicit "Search" button click; check the real HTML/page snapshot rather than assuming consistency across the app.
6. **Conditionally-rendered form fields** (Lapping/Slitting Charges, item Remarks, RIB Remarks — only appear for certain product codes or after certain other fields are filled) → always guard with a `count()`/`isVisible()` check before acting, never assume a field is present.
7. **Dynamic dates over hardcoded dates** — compute `poDate`/`scheduledDate`/etc. relative to `Date.now()` at runtime rather than hardcoding a JSON value, so tests don't silently go stale as the calendar moves forward.
8. **Long, real end-to-end flows can legitimately exceed Playwright's default 30s test timeout** even when every step succeeds — call `test.setTimeout(90_000)` (or similar) for multi-step real-server flows rather than chasing a phantom bug.

---

## 5. Performance / Concurrency Testing (separate from functional POM tests)

Three additional specs simulate multiple real users hitting the app **at the same time**, each in its own isolated browser context (not just parallel Playwright workers):

- `concurrent-users-load.spec.js` — 10 users log in and reach the dashboard concurrently (baseline).
- `concurrent-users-screens.spec.js` — 10 users each open a *different* screen concurrently (navigation-only).
- `concurrent-users-actions.spec.js` — 10 users each perform a *different real action* concurrently (Create Quotation, Create Deal, L1/L2 Approval, Planned Order, Production Order, Storage Assignment count, Dispatch Token creation, Pick List, Logistics Plans) — reusing the same page objects as the functional specs, with a mix of self-contained actions (always safe) and pipeline-dependent actions (need eligible live records, flagged accordingly).

**Honest scope note:** these are UI-level concurrency/functional tests, not formal load/performance testing (that requires a dedicated tool like JMeter/k6 hitting APIs directly at real scale with server-side metrics — a separate effort from this Playwright project).

---

## 6. How to Apply This Structure to a New Project

1. Build `BasePage.js` first — login, plant/tenant selection (if applicable), and shared navigation helpers everything else extends.
2. Map the real business flow end-to-end on paper first (like Section 2 above) — one module = one page object = one (or a few) spec file(s).
3. For any module chained to a previous module's output (an ID/number the app generates), decide up front: auto-wire it (read the previous stage's output file directly) or keep it a manual hand-off — and document which, so it doesn't turn into a surprise "why is this failing" investigation later.
4. Build page objects incrementally against real captured HTML/snapshots — never guess a selector when the real markup is available.
5. Keep long multi-screen flows (like Dispatch here) in **one spec file** even when split into multiple page objects, if that matches how a real user actually experiences it as one continuous session.
6. Add a testdata table (like Section 3) early, and mark clearly which fields are "must update every run" vs. static — this is the #1 recurring confusion point in a suite like this.
