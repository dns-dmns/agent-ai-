# 🤖 Complete AI QA Platform Architecture & Flow Documentation

**Last Updated:** July 29, 2026  
**Repository Workspace:** `D:\Ai`

---

## 🏗️ Platform Overview

The **Multi-Project AI QA Automation Platform** is a web-based testing platform for recording, generating, executing, and reporting enterprise test suites for web applications.

### Core Architecture Components:
1. **Chrome Playwright Recorder & Flow Creator (`Tab 1`)**:
   - Captures user interactions (`CLICK`, `TYPE`) and generates structured `actions.json` flow files.
2. **AI Test Generator & Execution Platform (`Tab 2`)**:
   - Single-Agent (`/analyze-flow`) & Multi-Agent Parallel Pipeline (`/generate-multi-agent-stream`):
     - **Agent 1 (The Builder)**: Generates CRUD, functional, and positive workflow scenarios.
     - **Agent 2 (The Breaker)**: Generates negative, edge-case, boundary, SQL injection, and script payload scenarios.
     - **Agent 3 (The Auditor)**: Evaluates a 25-point coverage matrix and performs gap analysis loops.
3. **Execution Engine (`/run-matrix-stream`)**:
   - Asynchronous Playwright runner supporting headed & headless execution modes.
   - Dynamic flow dependency chaining (`login`, `master_data`, etc.).
   - Visual close fallbacks for modal dialogs, backdrops, and PrimeNG overlays.
4. **Project Console & Workspace Statistics (`Tab 3`)**:
   - Independent project workspace creation (`Ambica`, `HRMS`, etc.).
   - Executive metrics dashboard and flow inventory table.

---

## ✅ Completed Tasks

1. **Fixed UI Card vs. Terminal Log Index Mismatch**:
   - Tagged each test scenario with `_original_id` prior to filtering so UI Card `#X` matches terminal log `#X` during filtered execution.
2. **Multi-Select Prerequisites & Scaling Roadmap Document**:
   - Implemented custom **Multi-Select Dropdown Component** for Chain Prerequisites / Dependencies in `dashboard.html`.
   - Bypassed browser GET caching on `/get-flow-details`, `/list-flows`, and `/list-projects`.
   - Created detailed 5-pillar architectural scaling roadmap saved at [AI_QA_Platform_Scaling_Roadmap.md](file:///d:/Ai/updates/AI_QA_Platform_Scaling_Roadmap.md).
3. **Visual Close Fallbacks for Modals & Dialogs**:
   - Implemented dynamic candidate locators for `Cancel`, `Close`, `No`, `Dismiss`, and modal `X` buttons (`.p-dialog-header-close`, `.btn-close`).
   - Handles unrecorded modal controls without breaking existing flows or requiring re-recording.
4. **Parallel Multi-Agent Generation (`asyncio.gather`)**:
   - Runs Agent 1 and Agent 2 simultaneously in parallel threads.
   - Reduces generation latency by 50% while maintaining identical test output quality.
5. **Test Type Checkbox Integration**:
   - Linked `1. Select Test Types to Filter:` checkboxes to both **🤖 Multi-Agent Generate** and **Run AI Post Analysis**.
6. **STARTUP_GUIDE & Infrastructure Support**:
   - Added `run_server.bat` launcher script to clear port 8000 conflicts.
   - Appended port conflict troubleshooting instructions in `STARTUP_GUIDE.txt`.

---

## ⏳ Pending Tasks

1. **Ambica Buglist Resolution (4 Items)**:
   - **Item 1 (Excel Export Full Dataset)**: Test scenario verifying Excel export contains full record count across all paginated records rather than single-page DOM limit.
   - **Item 2 (Beam Length Input Validation)**: Negative & zero boundary test scenarios for Beam Length Master input fields (`0`, `-10`, `abc`).
   - **Item 3 (Fabric Category Navigation & Pagination)**: Verification of post-submit navigation URL (`/fabric-category` vs `/sulzer-machine-master`) and 1st column pagination controls.
   - **Item 4 (Production Capacity Ghost Data Persistence)**: Verification that deleted/updated Production Capacity records do not persist after soft page refresh.
2. **User-Requested Follow-Ups**:
   - Execute Ambica Buglist fixes upon user instruction.

---

## 🎯 Current Task Status

* **Active Status**: Phase 5 Continuation & Execution Ready.
* **Details**: Master rebuild blueprint created at [v1.md](file:///d:/Ai/developmentface/v1.md), scaling roadmap at [AI_QA_Platform_Scaling_Roadmap.md](file:///d:/Ai/updates/AI_QA_Platform_Scaling_Roadmap.md), and execution strategy updated.

---

## 🚀 Next Implementation Phase & Continuation Plan

### 📌 Phase 5.1: Ambica Buglist Automated QA Verification Suites
- **Goal**: Generate and execute targeted Playwright AI test scenarios targeting the 4 Ambica enterprise bug reports.
- **Verification Strategy**:
  1. **Excel Full Export**: Trigger export click in `Product Type Master` / `FG Master` and inspect downloaded `.xlsx` row count against backend DB/table total count.
  2. **Beam Length Boundary Validation**: Execute Breaker Agent scenarios to enter invalid values (`0`, `-5`) into Beam Length form and verify validation toast message `.p-toast-message-error`.
  3. **Fabric Category Redirect Check**: Monitor `page.on('response')` and post-submit `page.url()` to ensure browser remains on `https://gc-dev.centralindia.cloudapp.azure.com/ambica/master-data/fabric-category`.
  4. **Production Capacity Persistence**: Add record $\rightarrow$ Verify table row $\rightarrow$ Hard refresh page (`page.reload()`) $\rightarrow$ Confirm row persistence.

### 📌 Phase 5.2: Scaling Roadmap Rollout (Pillars 1 - 5)
- [x] **Pillar 1 (Dynamic Variable Pool)**: Implemented in `qa_agent.py` via `resolve_dynamic_variables()`.
- [x] **Pillar 2 (Vision AI Selector Healing)**: Implemented via Gemini Vision fallback `vision_ai_locate_element()`.
- [x] **Pillar 3 (Fast API Pre-Seeding)**: Configured endpoint `POST /seed-prerequisite-api`.
- [x] **Pillar 4 (Smart DOM Mutation & Network Idle Observer)**: Implemented `wait_for_ui_idle()`.
- [x] **Pillar 5 (Self-Correcting Auto-Repair Loop)**: Integrated auto-healing loop `attempt_auto_healing()`.

---

## 🔑 Important Constraints & Guidelines

* **Strict Permission Requirement**: Never run Git commands (`git add`, `git commit`, `git push`) or modify codebase outside `D:\Ai` workspace without explicit prior permission from the user.
* **Accuracy First**: 100% test scenario accuracy and prompt quality is top priority over aggressive speed shortcuts.
* **Workflow Integrity**: Never break or modify existing recorded flows (`actions.json`, `flow.json`) or require user re-recordings.
* **Server Port**: `127.0.0.1:8000` is strictly local to the user's computer. Use `run_server.bat` to clear port conflicts.

