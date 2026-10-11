/* ═══════════════════════════════════════════════════════════════════════
   Defect log — an additional feature, deliberately disconnected.

   A defect sheet is a list of things already known to be broken, which is a
   different job from a generated suite. Nothing here touches a module's
   test_cases.json: defect scenarios get their own "<Flow> [defects]" flow, so
   a normal Run, the coverage numbers and the existing report are unchanged.

   The distinction that makes the report readable: an open defect that fails is
   not news, an open defect that passes is the fix landing, and a closed defect
   that fails is a regression.

   Like the other adapters, this only adds to app.js — it never edits it.
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v === undefined || v === null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");

    const projectQ = () => `project_name=${encodeURIComponent(window.activeProject())}`;

    async function api(url, options) {
        const response = await fetch(url, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
        return data;
    }

    const OUTCOME_LABEL = {
        still_open: ["Still open", "fail"],
        fixed: ["Fixed", "pass"],
        regressed: ["REGRESSED", "fail"],
        holding: ["Holding", "pass"],
        unverified: ["Unverified", "warn"],
        not_run: ["Not run", "idle"],
    };

    /* ══ The log ═════════════════════════════════════════════════════ */
    window.openDefectLog = async function () {
        if (!window.activeProject()) return window.toast("Open a workspace first", "error");
        let status;
        try {
            status = await api(`/defect-status?${projectQ()}`);
        } catch (e) {
            return window.toast(e.message, "error");
        }

        const rows = status.defects.map((d) => {
            const [label, tone] = OUTCOME_LABEL[d.outcome] || ["?", "idle"];
            return `
                <tr>
                    <td class="mono-sm">${esc(d.defect_id)}</td>
                    <td>${esc(d.module)}</td>
                    <td><span class="pill">${esc(d.priority)}</span></td>
                    <td>${esc(d.sheet_status)}</td>
                    <td><span class="status ${tone}">${esc(label)}</span></td>
                    <td class="mono-sm">${esc(d.reason.slice(0, 90))}</td>
                </tr>`;
        }).join("");

        const tally = Object.entries(status.summary || {}).map(([key, count]) => {
            const [label] = OUTCOME_LABEL[key] || [key];
            return `<div class="stat"><span class="stat-value">${count}</span>
                        <span class="stat-label">${esc(label.toLowerCase())}</span></div>`;
        }).join("");

        window.openModal("Defect log", `
            <p class="field-hint">Imported defects, re-tested against the application.
               These run in their own <code>[defects]</code> flows — your generated
               suites and their reports are untouched. A failing open defect is
               <em>still open</em>, not a regression; a passing one is the fix landing.</p>
            ${status.total ? `<div class="stat-row">${tally}</div>
            <table class="data-table">
                <thead><tr><th>ID</th><th>Module</th><th>Priority</th>
                    <th>Sheet</th><th>Last result</th><th>Why</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>`
            : `<p class="muted">No defect log imported yet.</p>`}
            <div class="field">
                <span class="field-label">Import a defect sheet</span>
                <input type="file" id="defectFileInput" accept=".csv,.tsv,.xlsx">
                <span class="field-hint">Needs a <strong>Module</strong> column and an
                   <strong>Expected Result</strong> column. Also reads Defect ID, Steps to
                   Reproduce, Actual Result, Priority and Status. CSV works with no extra
                   package; .xlsx needs <code>openpyxl</code>.</span>
            </div>
            <div id="defectImportOut"></div>`,
            `<button class="btn btn-primary" onclick="previewDefectImport()">Preview import</button>
             <button class="btn" onclick="rebuildDefectFlows()">Rebuild defect flows</button>
             ${status.total ? `<button class="btn btn-success" style="background:#059669;color:#fff" onclick="runAllDefectFlows()">▶ Test Defect Flows</button>` : ""}
             <button class="btn" style="background:#334155" onclick="closeModal()">Close</button>`);
    };

    /* ══ Preview: what would happen, nothing written ═════════════════ */
    window.previewDefectImport = async function () {
        const input = $("defectFileInput");
        if (!input || !input.files || !input.files[0]) {
            return window.toast("Choose a .csv or .xlsx file first", "error");
        }
        const body = new FormData();
        body.append("file", input.files[0]);
        const out = $("defectImportOut");
        out.innerHTML = '<div class="loading-row"><span class="spinner"></span> Reading…</div>';

        let data;
        try {
            data = await api(`/preview-defect-import?${projectQ()}`, { method: "POST", body });
        } catch (e) {
            out.innerHTML = `<div class="nudge"><span>!</span><span>${esc(e.message)}</span></div>`;
            return;
        }

        const runnable = data.planned.map((p) => `
            <tr><td class="mono-sm">${esc(p.defect_id)}</td>
                <td>${esc(p.flow)}</td>
                <td>${p.steps.length} steps</td>
                <td>${esc(p.expected_result.slice(0, 80))}</td></tr>`).join("");

        // Anything not runnable is shown with the reason — never quietly dropped,
        // and never turned into a scenario full of invented selectors.
        const blocked = data.blocked.map((b) => `
            <tr><td class="mono-sm">${esc(b.defect_id)}</td>
                <td>${esc(b.module)}</td>
                <td class="text-fail">${esc(b.reason.slice(0, 150))}</td></tr>`).join("");

        out.innerHTML = `
            <div class="stat-row">
                <div class="stat"><span class="stat-value">${data.total}</span>
                    <span class="stat-label">defects</span></div>
                <div class="stat"><span class="stat-value">${data.runnable}</span>
                    <span class="stat-label">runnable</span></div>
                <div class="stat"><span class="stat-value">${data.needs_input}</span>
                    <span class="stat-label">need input</span></div>
            </div>
            ${data.runnable ? `<div class="field">
                <span class="field-label">Will be tested</span>
                <table class="data-table"><thead><tr><th>ID</th><th>Flow</th>
                    <th>Steps</th><th>Expected</th></tr></thead>
                    <tbody>${runnable}</tbody></table></div>` : ""}
            ${data.needs_input ? `<div class="field">
                <span class="field-label text-fail">Cannot be tested yet</span>
                <table class="data-table"><thead><tr><th>ID</th><th>Module</th>
                    <th>Why</th></tr></thead><tbody>${blocked}</tbody></table>
                <span class="field-hint">A relative value ("less than the Inner
                   Diameter") has no single answer, and a module that was never
                   recorded has no known fields. Both are asked rather than guessed.
                   </span></div>` : ""}
            <div class="row-flex">
                <button class="btn btn-primary" onclick="confirmDefectImport()">
                    Import ${data.total} defect(s)</button>
            </div>`;
    };

    window.confirmDefectImport = async function () {
        const input = $("defectFileInput");
        if (!input || !input.files || !input.files[0]) {
            return window.toast("Choose the file again", "error");
        }
        const body = new FormData();
        body.append("file", input.files[0]);
        try {
            const result = await api(`/import-defects?${projectQ()}`, { method: "POST", body });
            window.toast(result.message, "success");
            await window.loadProjects?.();
            window.openDefectLog();
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    window.rebuildDefectFlows = async function () {
        try {
            const result = await api(`/rebuild-defect-flows?${projectQ()}`, { method: "POST" });
            window.toast(result.message, "success");
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    window.runAllDefectFlows = async function () {
        try {
            const data = await api(`/defect-flows?${projectQ()}`);
            const flows = data.defect_flows || [];
            if (!flows.length) {
                return window.toast("No defect flows available to run. Please import a defect sheet first.", "warn");
            }
            window.closeModal?.();
            window.toast(`Loading defect flow "${flows[0]}"...`, "info");

            const flowSelect = $("flowSelect");
            if (flowSelect) {
                flowSelect.value = flows[0];
                if (typeof window.loadSelectedFlow === "function") {
                    await window.loadSelectedFlow();
                }
            }
            // Switch to runner tab (Tab 3)
            const runnerTab = document.querySelectorAll(".rail-step")[2];
            if (typeof window.switchMainTab === "function") {
                window.switchMainTab("runner-view", runnerTab);
            }
            // Trigger suite execution
            if (typeof window.triggerTestSuite === "function") {
                window.triggerTestSuite();
            }
        } catch (e) {
            window.toast(e.message, "error");
        }
    };
})();
