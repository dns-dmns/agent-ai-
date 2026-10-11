/* Re-record a module, then review the changes before anything is replaced.

   - "Re-record an existing module" (Record tab) fills the flow name and start
     URL from the chosen module.
   - Stop & save is sent with review=true. When the module already has a
     recording, the server keeps the new one aside (<flow>/.pending/) and returns
     a comparison; this file shows it side by side -- old | new, with added,
     removed and changed steps -- and the tester chooses:
       Replace -> the new recording becomes the module's only one
       Keep old -> the new one is discarded
   - Opening a module that still has a recording waiting shows a banner to
     finish the review.
   app.js is not edited: its fetch to /stop-recording is wrapped.
*/
(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const project = () => ($("projectSelect") || {}).value || "";
    let pending = null;      // the review currently shown

    /* ── Re-record picker ─────────────────────────────────────────────── */
    function buildPicker() {
        const nameInput = $("flowNameInput");
        if (!nameInput || $("rerecordSelect")) return;
        const field = nameInput.closest(".field");
        const grid = field && field.parentElement;
        if (!grid) return;
        const wrap = document.createElement("div");
        wrap.className = "field";
        wrap.style.gridColumn = "1 / -1";
        wrap.innerHTML = `
            <label class="field-label" for="rerecordSelect">Re-record an existing module (optional)</label>
            <select id="rerecordSelect">
                <option value="">— New module —</option>
            </select>
            <span class="field-hint">Pick a module to record it again by hand. Before anything is
                replaced you will see the old and new recording side by side.</span>`;
        grid.insertBefore(wrap, grid.firstChild);
        $("rerecordSelect").addEventListener("change", onPick);
        fillPicker();
    }

    function fillPicker() {
        const select = $("rerecordSelect"), source = $("flowSelect");
        if (!select || !source) return;
        const keep = select.value;
        const names = [...source.querySelectorAll("optgroup option")].map((o) => o.value).filter(Boolean);
        select.innerHTML = `<option value="">— New module —</option>` +
            names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
        if (names.includes(keep)) select.value = keep;
    }

    async function onPick() {
        const name = $("rerecordSelect").value;
        if (!name) {                       // "New module": start from empty fields
            $("flowNameInput").value = "";
            $("targetUrlInput").value = "";
            return;
        }
        $("flowNameInput").value = name;
        try {
            const res = await fetch(`/get-flow-details?project_name=${encodeURIComponent(project())}` +
                                    `&flow_name=${encodeURIComponent(name)}`);
            const data = await res.json();
            const url = (data.flow || {}).target_url || "";
            if (url) $("targetUrlInput").value = url;
        } catch (e) { /* the tester can type the URL */ }
    }

    /* ── Stop & save with review ──────────────────────────────────────── */
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        if (!url.startsWith("/stop-recording")) return nativeFetch(input, init);
        const reviewUrl = url + (url.includes("?") ? "&" : "?") + "review=true";
        const response = await nativeFetch(reviewUrl, init);
        let data = null;
        try { data = await response.clone().json(); } catch (e) { return response; }
        if (data && data.status === "review") showReview(data);
        return response;
    };

    function row(r, side) {
        const s = r[side];
        if (!s) return `<td class="rr-empty">—</td>`;
        return `<td><span class="rr-action">${esc(s.action)}</span> ${esc(s.target)}` +
               `${s.value ? ` <span class="rr-value">“${esc(s.value)}”</span>` : ""}</td>`;
    }

    function showReview(data) {
        pending = data;
        const d = data.diff || {};
        const c = d.counts || {};
        const rows = d.rows || [];
        const list = (items) => items.length ? items.map(esc).join(", ") : "none";
        const body = `
            <p>New recording of <strong>${esc(data.flow_name)}</strong>: ${d.new_steps} steps
               (the current one has ${d.old_steps}). Nothing has been replaced yet.</p>
            <div class="rr-chips">
                <span class="rr-chip rr-added">${c.added || 0} added</span>
                <span class="rr-chip rr-removed">${c.removed || 0} removed</span>
                <span class="rr-chip rr-changed">${c.changed || 0} changed</span>
                <span class="rr-chip">${c.same || 0} unchanged</span>
            </div>
            <p class="dim">Fields only in the new recording: ${list(d.fields_added || [])}<br>
               Fields only in the old recording: ${list(d.fields_removed || [])}</p>
            <label class="dim"><input type="checkbox" id="rrShowSame"> Show unchanged steps</label>
            <div class="rr-scroll">
              <table class="rr-table">
                <thead><tr><th style="width:90px">Change</th><th>Old recording</th><th>New recording</th></tr></thead>
                <tbody>${rows.map((r) => `
                    <tr class="rr-${r.status}" ${r.status === "same" ? 'style="display:none"' : ""}>
                        <td><span class="rr-tag">${esc(r.status)}</span></td>${row(r, "old")}${row(r, "new")}
                    </tr>`).join("")}
                </tbody>
              </table>
            </div>`;
        const footer = `
            <button class="btn" onclick="confirmRecording('discard')">Keep old recording</button>
            <button class="btn btn-primary" onclick="confirmRecording('replace')">Replace with new recording</button>`;
        window.openModal("Review the new recording", body, footer);
        const toggle = $("rrShowSame");
        if (toggle) toggle.addEventListener("change", () => {
            document.querySelectorAll(".rr-table tr.rr-same").forEach((tr) => {
                tr.style.display = toggle.checked ? "" : "none";
            });
        });
    }

    window.confirmRecording = async function (decision) {
        if (!pending) return;
        const q = `project_name=${encodeURIComponent(pending.project_name)}` +
                  `&flow_name=${encodeURIComponent(pending.flow_name)}&decision=${decision}`;
        const res = await nativeFetch(`/confirm-recording?${q}`, { method: "POST" });
        const data = await res.json().catch(() => ({}));
        window.closeModal();
        if (window.toast) window.toast(data.message || data.detail || "Done", res.ok ? "ok" : "error");
        const name = pending.flow_name;
        pending = null;
        hideBanner();
        if (typeof window.fetchSavedFlows === "function") await window.fetchSavedFlows(name);
    };

    /* ── A review left unfinished ─────────────────────────────────────── */
    function hideBanner() { const b = $("rrBanner"); if (b) b.remove(); }

    async function checkPending(flowName) {
        hideBanner();
        if (!flowName) return;
        try {
            const res = await nativeFetch(`/recording-review?project_name=${encodeURIComponent(project())}` +
                                          `&flow_name=${encodeURIComponent(flowName)}`);
            const data = await res.json();
            if (data.status !== "review") return;
            const host = document.querySelector(".main") || document.body;
            const banner = document.createElement("div");
            banner.id = "rrBanner";
            banner.className = "nudge";
            banner.style.margin = "var(--space-4) var(--space-6) 0";
            banner.innerHTML = `<span>!</span><span>A new recording of <strong>${esc(flowName)}</strong>
                is waiting for your review. <button class="btn btn-sm" id="rrOpen">Review now</button></span>`;
            host.insertBefore(banner, host.firstChild);
            $("rrOpen").addEventListener("click", () => showReview(data));
        } catch (e) { /* no banner */ }
    }

    function after(name, fn) {
        const original = window[name];
        if (typeof original !== "function") return;
        window[name] = function (...args) {
            const result = original.apply(this, args);
            Promise.resolve(result).then(() => { try { fn(args); } catch (e) {} });
            return result;
        };
    }
    /* The Record tab follows the module open in the sidebar -- it showed the
       last module picked in the Record tab instead (Inventory while Arrival was
       open). Never while a recording browser is open: that session's name and
       URL must not change under the tester. */
    function followSelection(flowName) {
        const select = $("rerecordSelect"), launch = $("launchBtn");
        if (!select || !flowName || (launch && launch.disabled)) return;
        if (![...select.options].some((o) => o.value === flowName)) fillPicker();
        if (select.value === flowName) return;
        select.value = flowName;
        onPick();
    }

    after("fetchSavedFlows", () => fillPicker());
    after("loadSelectedFlow", () => {
        const flowName = ($("flowSelect") || {}).value;
        followSelection(flowName);
        checkPending(flowName);
    });

    buildPicker();
})();
