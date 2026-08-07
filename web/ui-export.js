/* ═══════════════════════════════════════════════════════════════════════
   Export the project as a standalone Playwright POM suite.

   A separate feature, and strictly read-only: it asks the server to generate
   files from what has already been recorded and streams them back as a zip.
   Nothing is written to a flow, so exporting — or never exporting — has no
   effect on recording, generation, running or reports.

   Like the other adapters this only adds to app.js; it never edits it.
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v === undefined || v === null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");

    const projectQ = () => `project_name=${encodeURIComponent(window.activeProject())}`;

    async function api(url) {
        const response = await fetch(url);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
        return data;
    }

    const kb = (bytes) => bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;

    window.openProjectExport = async function () {
        if (!window.activeProject()) return window.toast("Open a workspace first", "error");
        let data;
        try {
            window.toast("Generating the project…", "info");
            data = await api(`/preview-export?${projectQ()}`);
        } catch (e) {
            return window.toast(e.message, "error");
        }

        // Group the flat file list into its folders, which is how a tester will
        // read it when they unzip it.
        const groups = {};
        data.files.forEach((f) => {
            const folder = f.path.includes("/") ? f.path.split("/")[0] : "(root)";
            (groups[folder] = groups[folder] || []).push(f);
        });
        const tree = Object.keys(groups).sort().map((folder) => `
            <div class="spec-block">
                <strong class="mono-sm">${esc(folder)}/</strong>
                <table class="data-table"><tbody>${
                    groups[folder].map((f) => `
                        <tr><td class="mono-sm">${esc(f.path.split("/").pop())}</td>
                            <td class="mono-sm">${kb(f.bytes)}</td></tr>`).join("")
                }</tbody></table>
            </div>`).join("");

        const modules = data.modules.map((m) => `
            <tr>
                <td>${esc(m.flow)}</td>
                <td>${m.scenarios}</td>
                <td>${m.fields}</td>
                <td>${m.exported
                    ? '<span class="status pass">exported</span>'
                    : '<span class="status idle">no scenarios</span>'}</td>
            </tr>`).join("");

        window.openModal(`Export — ${window.activeProject()}`, `
            <p class="field-hint">A standalone Playwright project: one page object per
               module with the locators, one spec per module that only calls those
               methods, and a test-data file per module. Generated from the recording
               and the scenarios — no model is involved, so the same project always
               produces the same code. <strong>Nothing is written to your flows.</strong></p>

            <div class="stat-row">
                <div class="stat"><span class="stat-value">${data.modules.length}</span>
                    <span class="stat-label">modules</span></div>
                <div class="stat"><span class="stat-value">${data.total_scenarios}</span>
                    <span class="stat-label">scenarios</span></div>
                <div class="stat"><span class="stat-value">${data.total_files}</span>
                    <span class="stat-label">files</span></div>
            </div>

            <div class="field">
                <span class="field-label">Modules</span>
                <table class="data-table">
                    <thead><tr><th>Flow</th><th>Scenarios</th><th>Fields</th>
                        <th>Status</th></tr></thead>
                    <tbody>${modules}</tbody>
                </table>
                ${data.skipped.length ? `<span class="field-hint">Skipped, because
                   nothing has been generated for them yet:
                   ${esc(data.skipped.join(", "))}.</span>` : ""}
            </div>

            <div class="field">
                <span class="field-label">Files</span>
                ${tree}
            </div>

            <details class="spec-block">
                <summary><strong>Preview <code>${esc(data.sample_path)}</code></strong></summary>
                <pre class="mono-sm" style="white-space:pre-wrap">${esc(data.sample)}</pre>
            </details>
        `,
            `<button class="btn btn-primary" onclick="downloadProjectZip()">
                Download .zip</button>
             <button class="btn" style="background:#334155" onclick="closeModal()">Close</button>`);
    };

    window.downloadProjectZip = function () {
        // A plain navigation, so the browser's own download UI handles it and the
        // Content-Disposition filename is respected.
        window.location.href = `/download-project?${projectQ()}`;
        window.toast("Building the zip…", "success");
    };
})();
