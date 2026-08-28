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
})();
