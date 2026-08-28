/* User-authored test cases — the generate tab without the agents.
 *
 * Wires three things to the "Write your own test cases" card in the Generate
 * view: the module inventory ("what is there in the application"), the CSV
 * template download, and the upload of a filled template. Follows the same
 * adapter pattern as ui-coverage.js: app.js is never edited, helpers stay
 * local to this file, and the suite list is refreshed through the public
 * loadSelectedFlow() after an import.
 */
(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v === undefined || v === null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

    const flowQ = () =>
        `project_name=${encodeURIComponent(window.activeProject())}` +
        `&flow_name=${encodeURIComponent(window.activeFlow())}`;

    async function api(url, options) {
        const res = await fetch(url, options);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        return data;
    }

    /* ── "What's in this module?" ───────────────────────────────────── */
    window.openModuleInventory = async function () {
        if (!window.activeFlow()) return window.toast("Load a module first", "error");
        let data;
        try {
            data = await api(`/module-inventory?${flowQ()}`);
        } catch (e) {
            return window.toast(e.message, "error");
        }

        const fieldRows = (data.fields || []).map((f) => `
            <tr>
                <td>${esc(f.label)}${f.required ? ' <span class="tag">required</span>' : ""}
                    ${f.sensitive ? ' <span class="tag">sensitive</span>' : ""}</td>
                <td class="mono-sm dim">${esc(f.kind)}</td>
                <td class="mono-sm dim">${f.options && f.options.length
                    ? esc(f.options.join(" · ")) : "—"}</td>
            </tr>`).join("");

        const buttonRows = (data.buttons || []).map((b) =>
            `<span class="tag">${esc(b.text || b.selector)}</span>`).join(" ");

        const messages = (data.observed_messages || []).map((m) =>
            `<li>“${esc(m)}”</li>`).join("");

        window.openModal(`This module — ${esc(window.activeFlow())}`, `
            <p class="dim">Use the field names below as column headers in the template.
               Values you write are filled automatically — you never write selectors or
               steps, and the outcome is checked on screen by the verdict engine.</p>
            ${data.fields && data.fields.length ? `
            <table class="table">
                <thead><tr><th>Field</th><th>Type</th><th>Choices</th></tr></thead>
                <tbody>${fieldRows}</tbody>
            </table>` : `<p class="muted">No fields are known yet — record the module's
                form (open it and fill the fields), or run the module once so the engine
                can inventory the page.</p>`}
            ${buttonRows ? `<p class="dim" style="margin-top:var(--space-3)">
                Buttons seen while recording: ${buttonRows}</p>` : ""}
            ${messages ? `<p class="dim" style="margin-top:var(--space-3)">
                Messages the application really showed — quote these in
                Expected Result:</p><ul class="dim">${messages}</ul>` : ""}
        `, `
            <button class="btn btn-primary" onclick="downloadUserCaseTemplate()">
                Download the template (CSV)</button>
            <button class="btn" style="background:#334155" onclick="closeModal()">Close</button>
        `);
    };

    /* ── Template download ──────────────────────────────────────────── */
    window.downloadUserCaseTemplate = async function () {
        if (!window.activeFlow()) return window.toast("Load a module first", "error");
        try {
            // Probe first so a 400 (no fields yet) surfaces as a readable toast
            // instead of a broken download tab.
            const res = await fetch(`/user-case-template?${flowQ()}`);
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || `HTTP ${res.status}`);
            }
            const blob = await res.blob();
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            const disposition = res.headers.get("Content-Disposition") || "";
            const named = /filename="([^"]+)"/.exec(disposition);
            link.download = named ? named[1] : "test_cases_template.csv";
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(link.href);
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    /* ── Upload a filled template ───────────────────────────────────── */
    window.pickUserCasesFile = () => $("userCasesUploadInput").click();

    window.uploadUserCases = async function (input) {
        const file = input.files && input.files[0];
        if (!file) return;
        input.value = "";                       // same file can be re-picked
        if (!window.activeFlow()) return window.toast("Load a module first", "error");

        const body = new FormData();
        body.append("file", file, file.name);
        let result;
        try {
            result = await api(`/upload-user-cases?${flowQ()}`, { method: "POST", body });
        } catch (e) {
            return window.toast(e.message, "error");
        }

        const host = $("userCasesResult");
        if (host) {
            const blockedRows = (result.blocked || []).map((b) => `
                <div class="nudge"><span>!</span>
                    <span><b>Row ${b.row}</b> — ${esc(b.scenario)}: ${esc(b.reason)}</span>
                </div>`).join("");
            const unresolved = (result.unresolved_columns || []).length
                ? `<div class="dim mono-sm">Ignored column header(s):
                     ${esc(result.unresolved_columns.join(", "))}</div>` : "";
            host.innerHTML = `
                <div>${esc(result.message)}</div>
                ${blockedRows}${unresolved}`;
        }
        window.toast(result.message, (result.blocked || []).length ? "info" : "success");
        await window.loadSelectedFlow?.();
    };

    /* ══ Accurate-data worksheet → generated cases (playbook Step 6) ══ */
    let pendingWorksheet = null;             // the File awaiting a confirmed import

    function downloadCsv(res, fallbackName) {
        return res.blob().then((blob) => {
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            const disp = res.headers.get("Content-Disposition") || "";
            const named = /filename="([^"]+)"/.exec(disp);
            link.download = named ? named[1] : fallbackName;
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(link.href);
        });
    }

    window.downloadWorksheet = async function () {
        if (!window.activeFlow()) return window.toast("Load a module first", "error");
        try {
            const res = await fetch(`/worksheet-template?${flowQ()}`);
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || `HTTP ${res.status}`);
            }
            await downloadCsv(res, "data_worksheet.csv");
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    window.pickWorksheetFile = () => $("worksheetUploadInput").click();

    window.previewWorksheet = async function (input) {
        const file = input.files && input.files[0];
        if (!file) return;
        input.value = "";
        if (!window.activeFlow()) return window.toast("Load a module first", "error");
        pendingWorksheet = file;

        const body = new FormData();
        body.append("file", file, file.name);
        let data;
        try {
            data = await api(`/preview-worksheet?${flowQ()}`, { method: "POST", body });
        } catch (e) {
            return window.toast(e.message, "error");
        }

        const caseRows = (data.cases || []).map((c) => `
            <tr>
                <td>${esc(c.scenario_name)}</td>
                <td class="mono-sm dim">${esc(c.type)}</td>
                <td class="dim">${esc(c.expected_result)}</td>
            </tr>`).join("");
        const notes = (data.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
        const unused = (data.captured_unused || []).map((n) =>
            `<li>${esc(n)}</li>`).join("");
        const blocked = (data.blocked || []).map((b) =>
            `<div class="nudge"><span>!</span><span><b>Row ${b.row}</b> —
             ${esc(b.scenario)}: ${esc(b.reason)}</span></div>`).join("");
        const unmatched = (data.unmatched_fields || []).length
            ? `<p class="dim mono-sm">Worksheet field(s) the module does not
                 recognise (ignored): ${esc(data.unmatched_fields.join(", "))}</p>` : "";

        window.openModal(`Cases from the worksheet — ${esc(window.activeFlow())}`, `
            <p class="dim">${data.case_count} scenario(s) generated from
               ${data.messages_harvested} harvested message(s). Review, then import —
               steps and the on-screen check are added automatically.</p>
            ${caseRows ? `<table class="table"><thead><tr><th>Scenario</th><th>Type</th>
                <th>Expected result</th></tr></thead><tbody>${caseRows}</tbody></table>`
                : `<p class="muted">Nothing was generated — fill the MESSAGE and DATA
                   rows and try again.</p>`}
            ${notes ? `<p class="dim" style="margin-top:var(--space-3)">Notes:</p>
                <ul class="dim">${notes}</ul>` : ""}
            ${unused ? `<p class="dim">Captured but not turned into cases:</p>
                <ul class="dim">${unused}</ul>` : ""}
            ${unmatched}${blocked}
        `, `
            <button class="btn btn-primary" onclick="confirmWorksheetImport()"
                ${data.case_count ? "" : "disabled"}>Import ${data.case_count} case(s)</button>
            <button class="btn" style="background:#334155" onclick="closeModal()">Cancel</button>
        `);
    };

    window.confirmWorksheetImport = async function () {
        if (!pendingWorksheet) return;
        const body = new FormData();
        body.append("file", pendingWorksheet, pendingWorksheet.name);
        let result;
        try {
            result = await api(`/import-worksheet?${flowQ()}`, { method: "POST", body });
        } catch (e) {
            return window.toast(e.message, "error");
        }
        pendingWorksheet = null;
        window.closeModal?.();
        window.toast(result.message, "success");
        const host = $("userCasesResult");
        if (host) host.innerHTML = `<div>${esc(result.message)}</div>`;
        await window.loadSelectedFlow?.();
    };
})();
