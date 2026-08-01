/* ═══════════════════════════════════════════════════════════════════════
   Rules, dependency contracts and the test matrix.

   The surfaces for the two things a recording can never tell the platform:
   what you know about the application, and how its modules depend on each
   other. Like ui.js, this only wraps app.js — it never edits it.
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v === undefined || v === null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");

    // app.js keeps currentProjectName / currentFlowName in closure scope, so the
    // hidden selects are the reliable way to read them.
    window.activeProject = () => $("projectSelect")?.value || "";
    window.activeFlow = () => $("flowSelect")?.value || "";

    const flowQ = () =>
        `project_name=${encodeURIComponent(window.activeProject())}` +
        `&flow_name=${encodeURIComponent(window.activeFlow())}`;
    const projectQ = () => `project_name=${encodeURIComponent(window.activeProject())}`;

    async function api(url, options) {
        const response = await fetch(url, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
        return data;
    }

    /* ══ Rules ═══════════════════════════════════════════════════════ */
    window.openRulesManager = async function () {
        if (!window.activeProject()) return window.toast("Open a workspace first", "error");
        try {
            renderRulesModal(await api(`/get-rules?${flowQ()}`), window.activeFlow());
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    function ruleLine(rule, scope) {
        const facts = [
            rule.selector && `field ${rule.selector}`,
            rule.values && rule.values.length && `values ${rule.values.join(", ")}`,
            rule.value && `"${rule.value}"`,
            rule.url_contains && `url ~ ${rule.url_contains}`,
            rule.min_rows && `min ${rule.min_rows} rows`,
        ].filter(Boolean).join("  ·  ");

        const payload = encodeURIComponent(JSON.stringify(rule));
        return `
            <div class="scenario" style="grid-template-columns:auto minmax(0,1fr) auto">
                <span class="glyph ${rule.enabled ? "pass" : "idle"}">§</span>
                <div class="scenario-main">
                    <div class="scenario-title">
                        <span>${esc(rule.title)}</span>
                        <span class="tag">${esc(rule.kind)}</span>
                        <span class="tag">${esc(rule.severity)}</span>
                        <span class="tag">${scope}</span>
                        ${rule.generate_scenario ? '<span class="tag">generates a test</span>' : ""}
                    </div>
                    ${rule.text ? `<div class="scenario-expected">${esc(rule.text)}</div>` : ""}
                    ${facts ? `<div class="dim mono-sm">${esc(facts)}</div>` : ""}
                </div>
                <div class="row-flex">
                    <button class="btn btn-ghost btn-sm" title="Edit"
                        onclick="editRule('${payload}','${scope}')">&#9998;</button>
                    <button class="btn btn-ghost btn-sm" title="Delete"
                        onclick="removeRule('${esc(rule.id)}','${scope}')">&#128465;</button>
                </div>
            </div>`;
    }

    function renderRulesModal(data, flow) {
        const projectRules = data.project_rules || [];
        const flowRules = data.flow_rules || [];
        const placeholder = [
            "One rule per line, for example:",
            "After saving a Fabric Category it must stay on /fabric-category",
            "Beam Length must reject 0 and negative values",
            "The Excel export must contain every record, not just the visible page",
        ].join("\n");

        window.openModal("&sect; Rules", `
            <p class="muted" style="font-size:var(--text-sm)">
                A rule is something you know that no recording can show. Each one is
                <strong>enforced on every scenario</strong>, fed into the generation agents,
                and can produce its own test case.
            </p>

            <div class="card">
                <div class="card-head"><span class="card-title">Describe rules in plain English</span></div>
                <div class="card-body">
                    <textarea id="ruleText" rows="4" placeholder="${esc(placeholder)}"></textarea>
                    <div class="row-flex">
                        <button class="btn btn-primary" onclick="translateRules()">Convert to rules</button>
                        <span class="dim mono-sm">Nothing is saved until you review it.</span>
                    </div>
                    <div id="ruleDraftArea" class="stack-sm"></div>
                </div>
            </div>

            <div class="card">
                <div class="card-head">
                    <span class="card-title">This flow${flow ? ": " + esc(flow) : ""}</span>
                    <span class="spacer"></span><span class="tag">${flowRules.length}</span>
                </div>
                ${flowRules.length ? flowRules.map((r) => ruleLine(r, "flow")).join("")
                    : '<div class="empty"><div class="empty-text dim">No rules for this flow yet.</div></div>'}
            </div>

            <div class="card">
                <div class="card-head">
                    <span class="card-title">Whole workspace</span>
                    <span class="spacer"></span><span class="tag">${projectRules.length}</span>
                </div>
                ${projectRules.length ? projectRules.map((r) => ruleLine(r, "project")).join("")
                    : '<div class="empty"><div class="empty-text dim">No workspace-wide rules yet.</div></div>'}
            </div>`,
            `<button class="btn" onclick="editRule('','flow')">+ Add manually</button>
             <button class="btn" style="background:#334155" onclick="closeModal()">Close</button>`);

        setRulesBadge(projectRules.length + flowRules.length);
    }

    function setRulesBadge(count) {
        const badge = $("rulesCountBadge");
        if (badge) badge.textContent = count ? String(count) : "";
    }

    window.translateRules = async function () {
        const text = ($("ruleText").value || "").trim();
        if (!text) return window.toast("Write at least one rule first", "error");
        const area = $("ruleDraftArea");
        area.innerHTML = '<div class="loading-row"><span class="spinner"></span> Converting…</div>';
        try {
            const data = await api(`/translate-rules?${flowQ()}`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text }),
            });
            area.innerHTML = `
                <div class="nudge" style="background:var(--accent-quiet)">
                    <span>✓</span>
                    <span>${data.rules.length} rule(s) drafted — check them, then save.</span>
                </div>
                <textarea id="ruleDraftJson" rows="12" class="mono-sm">${
                    esc(JSON.stringify(data.rules, null, 2))}</textarea>
                <div class="row-flex">
                    <button class="btn btn-primary" onclick="saveDraftRules()">Save these rules</button>
                    <button class="btn" onclick="document.getElementById('ruleDraftArea').innerHTML=''">
                        Discard</button>
                </div>`;
        } catch (e) {
            area.innerHTML = `<div class="nudge" style="background:var(--fail-quiet)">
                <span>✕</span><span>${esc(e.message)}</span></div>`;
        }
    };

    window.saveDraftRules = async function () {
        let drafted;
        try {
            drafted = JSON.parse($("ruleDraftJson").value);
        } catch (e) {
            return window.toast("Invalid JSON: " + e.message, "error");
        }
        try {
            const data = await api(`/save-rules?${flowQ()}`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ rules: drafted }),
            });
            window.toast(`${data.saved} rule(s) saved`, "ok");
            window.openRulesManager();
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    window.editRule = function (encoded, scope) {
        const rule = encoded ? JSON.parse(decodeURIComponent(encoded)) : {
            title: "", text: "", kind: "must_show", severity: "major",
            selector: "", value: "", values: [], url_contains: "", min_rows: null,
            generate_scenario: true, enabled: true,
        };
        window.openModal(encoded ? "Edit rule" : "New rule", `
            <div class="field">
                <span class="field-label">Rule</span>
                <textarea id="ruleEditJson" rows="15" class="mono-sm">${
                    esc(JSON.stringify(rule, null, 2))}</textarea>
                <span class="field-hint">kinds: must_show · must_not_show · must_navigate ·
                    must_not_navigate · field_rejects · field_accepts · export_complete ·
                    no_console_errors · row_persists</span>
            </div>
            <div class="field">
                <span class="field-label">Applies to</span>
                <select id="ruleScope">
                    <option value="flow" ${scope === "flow" ? "selected" : ""}>This flow only</option>
                    <option value="project" ${scope === "project" ? "selected" : ""}>Whole workspace</option>
                </select>
            </div>
            <div id="ruleEditError" class="text-fail mono-sm"></div>`,
            `<button class="btn btn-primary" onclick="saveSingleRule()">Save rule</button>
             <button class="btn" style="background:#334155" onclick="openRulesManager()">Back</button>`);
    };

    window.saveSingleRule = async function () {
        let rule;
        try {
            rule = JSON.parse($("ruleEditJson").value);
        } catch (e) {
            $("ruleEditError").textContent = "Invalid JSON: " + e.message;
            return;
        }
        const query = $("ruleScope").value === "flow" ? flowQ() : projectQ();
        try {
            await api(`/save-rule?${query}`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(rule),
            });
            window.toast("Rule saved", "ok");
            window.openRulesManager();
        } catch (e) {
            $("ruleEditError").textContent = e.message;
        }
    };

    window.removeRule = async function (ruleId, scope) {
        if (!confirm("Delete this rule?")) return;
        const query = scope === "flow" ? flowQ() : projectQ();
        try {
            await api(`/delete-rule?${query}&rule_id=${encodeURIComponent(ruleId)}`,
                { method: "POST" });
            window.openRulesManager();
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    /* ══ Dependency contracts ════════════════════════════════════════ */
    window.openContractEditor = async function () {
        if (!window.activeFlow()) return window.toast("Select a flow first", "error");
        const deps = $("profileDependencies") ? $("profileDependencies").value : "";
        try {
            const data = await api(
                `/get-contract?${flowQ()}&dependencies=${encodeURIComponent(deps)}`);
            const providers = Object.entries(data.providers || {});
            const unresolved = data.unresolved || [];

            window.openModal("&#8646; Dependencies — " + esc(window.activeFlow()), `
                <p class="muted" style="font-size:var(--text-sm)">
                    A contract says what this flow <strong>provides</strong> to other modules and what
                    it <strong>requires</strong> from them. With it, a prerequisite's value is captured
                    and reused here as <code>{{variable}}</code> instead of being guessed.
                </p>

                ${providers.length ? `
                <div class="card">
                    <div class="card-head"><span class="card-title">Provided by your prerequisites</span></div>
                    <div class="card-body stack-sm">
                        ${providers.map(([entity, info]) => `
                            <div class="row-flex">
                                <span class="tag">${esc(entity)}</span>
                                <span class="dim mono-sm">from ${esc(info.flow)}
                                    &rarr; {{${esc(info.provide.var)}}}</span>
                            </div>`).join("")}
                    </div>
                </div>` : ""}

                ${unresolved.length ? `
                <div class="nudge"><span>!</span><span>
                    ${unresolved.length} requirement(s) have no provider:
                    ${unresolved.map((u) => esc(u.entity)).join(", ")}. Add that master to
                    Chain prerequisites and give it a contract that provides the entity.
                </span></div>` : ""}

                <div class="field">
                    <span class="field-label">Contract</span>
                    <textarea id="contractJson" rows="15" class="mono-sm">${
                        esc(JSON.stringify(data.contract, null, 2))}</textarea>
                    <span class="field-hint">provides[]: entity, var, name_field, status_field,
                        list_url &nbsp;·&nbsp; requires[]: entity, selector, kind &nbsp;·&nbsp; run_once</span>
                </div>
                <div id="contractError" class="text-fail mono-sm"></div>`,
                `<button class="btn btn-primary" onclick="saveContract()">Save contract</button>
                 <button class="btn" onclick="previewRelationshipChecks()">Preview checks</button>
                 <button class="btn" style="background:#334155" onclick="closeModal()">Close</button>`);
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    window.saveContract = async function () {
        let contract;
        try {
            contract = JSON.parse($("contractJson").value);
        } catch (e) {
            $("contractError").textContent = "Invalid JSON: " + e.message;
            return;
        }
        try {
            await api(`/save-contract?${flowQ()}`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(contract),
            });
            window.toast("Contract saved", "ok");
            window.openContractEditor();
        } catch (e) {
            $("contractError").textContent = e.message;
        }
    };

    window.previewRelationshipChecks = async function () {
        const deps = $("profileDependencies") ? $("profileDependencies").value : "";
        try {
            const data = await api(
                `/preview-relationship-checks?${flowQ()}&dependencies=${encodeURIComponent(deps)}`);
            window.openModal("&#8646; Generated dependency checks", `
                <p class="muted" style="font-size:var(--text-sm)">
                    These test the relationship <em>between</em> modules — dropdown membership,
                    inactive exclusion and delete-in-use — not one module alone.
                </p>
                ${(data.notes || []).map((n) =>
                    `<div class="nudge"><span>!</span><span>${esc(n)}</span></div>`).join("")}
                ${(data.scenarios || []).length ? `<div class="card">${
                    data.scenarios.map((s) => `
                        <div class="scenario" style="grid-template-columns:auto minmax(0,1fr)">
                            <span class="glyph idle">&#8646;</span>
                            <div class="scenario-main">
                                <div class="scenario-title">${esc(s.scenario_name)}
                                    <span class="tag">${esc(s.type)}</span></div>
                                <div class="scenario-expected">${esc(s.expected_result)}</div>
                            </div>
                        </div>`).join("")}</div>`
                    : '<div class="empty"><div class="empty-text dim">Nothing to generate yet.</div></div>'}`,
                `${(data.scenarios || []).length ? `<button class="btn btn-primary"
                    onclick="addRelationshipChecks()">Add ${data.scenarios.length} to the suite</button>`
                    : ""}
                 <button class="btn" style="background:#334155" onclick="openContractEditor()">Back</button>`);
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    window.addRelationshipChecks = async function () {
        const deps = $("profileDependencies") ? $("profileDependencies").value : "";
        try {
            const data = await api(
                `/add-relationship-checks?${flowQ()}&dependencies=${encodeURIComponent(deps)}`,
                { method: "POST" });
            window.closeModal();
            window.toast(`${data.added} check(s) added to the suite`, "ok");
            window.loadSelectedFlow();
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    /* ══ Test matrix ═════════════════════════════════════════════════ */
    window.downloadTestMatrix = function (fmt) {
        if (!window.activeProject()) return;
        window.location = `/export-test-matrix?${flowQ()}&fmt=${fmt || "csv"}`;
    };

    /* ══ Rules summary on the report ═════════════════════════════════ */
    function renderRulesSummary(report) {
        const section = $("rulesSection");
        const host = $("rulesSummaryList");
        if (!section || !host) return;
        const summary = (report && report.rules_enforced) || [];
        if (!summary.length) {
            section.classList.add("hidden");
            return;
        }
        section.classList.remove("hidden");
        host.innerHTML = summary.map((rule) => `
            <div class="row-flex">
                <span class="status ${rule.violations ? "fail" : "pass"}">${
                    rule.violations ? "violated" : "holds"}</span>
                <span>${esc(rule.title)}</span>
                <span class="dim mono-sm">checked in ${rule.checked} scenario(s)</span>
                ${rule.violations ? `<span class="dim mono-sm">— ${
                    esc((rule.examples[0] || {}).detail || "")}</span>` : ""}
            </div>`).join("");
    }

    const originalRestore = window.restoreExecutionReport;
    if (typeof originalRestore === "function") {
        window.restoreExecutionReport = function (report) {
            const result = originalRestore.apply(this, arguments);
            try { renderRulesSummary(report); } catch (e) { /* optional */ }
            return result;
        };
    }

    /* ══ Extra run options ═══════════════════════════════════════════ */
    const originalTrigger = window.triggerTestSuite;
    if (typeof originalTrigger === "function") {
        window.triggerTestSuite = function () {
            // The base implementation builds its own stream URL, so the new options
            // are appended by intercepting exactly one EventSource construction.
            const Native = window.EventSource;
            const extras =
                `&seed_once=${$("seedOnceCheck") ? $("seedOnceCheck").checked : true}` +
                `&relationship_checks=${$("relationshipCheck") ? $("relationshipCheck").checked : false}` +
                `&apply_rules=${$("applyRulesCheck") ? $("applyRulesCheck").checked : true}`;
            window.EventSource = function (url, options) {
                window.EventSource = Native;
                return new Native(
                    String(url).includes("/run-matrix-stream") ? url + extras : url, options);
            };
            try {
                return originalTrigger.apply(this, arguments);
            } finally {
                window.EventSource = Native;
            }
        };
    }

    /* ══ Roles — creator, approver and operator are different people ══
       SWITCH_ROLE reads projects/<p>/config.json → roles. The base settings
       modal has no field for them, so the section is appended to it here and
       merged into the save payload by intercepting that one request. */
    let roleRows = [];

    function renderRoleRows() {
        const host = $("rolesRows");
        if (!host) return;
        host.innerHTML = roleRows.length ? roleRows.map((role, index) => `
            <div class="grid-3" style="align-items:end">
                <div class="field">
                    <label class="field-label">Role name</label>
                    <input value="${esc(role.name)}" placeholder="approver"
                        oninput="updateRoleField(${index},'name',this.value)">
                </div>
                <div class="field">
                    <label class="field-label">Username</label>
                    <input value="${esc(role.username)}"
                        oninput="updateRoleField(${index},'username',this.value)">
                </div>
                <div class="row-flex">
                    <div class="field" style="flex:1">
                        <label class="field-label">Password</label>
                        <input type="password" value="${esc(role.password)}"
                            oninput="updateRoleField(${index},'password',this.value)">
                    </div>
                    <button class="btn btn-ghost btn-sm" title="Remove"
                        onclick="removeRoleRow(${index})">🗑</button>
                </div>
            </div>`).join("")
            : `<div class="dim mono-sm">No roles yet. Add one to use
               <code>SWITCH_ROLE</code> in a scenario.</div>`;
    }

    window.updateRoleField = (index, field, value) => { roleRows[index][field] = value; };
    window.removeRoleRow = (index) => { roleRows.splice(index, 1); renderRoleRows(); };
    window.addRoleRow = () => {
        roleRows.push({ name: "", username: "", password: "" });
        renderRoleRows();
    };

    async function appendRolesSection() {
        const body = $("genericModalBody");
        if (!body || $("rolesRows")) return;
        let config = {};
        try {
            config = await api(`/get-project-config?${projectQ()}`);
        } catch (e) { /* the base modal already reported any failure */ }
        roleRows = Object.entries(config.roles || {}).map(([name, creds]) => ({
            name, username: creds.username || "", password: creds.password || "",
        }));

        const card = document.createElement("div");
        card.className = "card";
        card.innerHTML = `
            <div class="card-head">
                <span class="card-title">Roles</span>
                <span class="spacer"></span>
                <button class="btn btn-sm" onclick="addRoleRow()">+ Add role</button>
            </div>
            <div class="card-body">
                <div class="dim mono-sm">Used by <code>SWITCH_ROLE approver</code> in a scenario —
                    each role keeps its own session, so one run can create a record as one user and
                    approve it as another.</div>
                <div id="rolesRows" class="stack"></div>
            </div>`;
        body.appendChild(card);
        renderRoleRows();
    }

    const originalSettings = window.openProjectSettings;
    if (typeof originalSettings === "function") {
        window.openProjectSettings = async function () {
            const result = await originalSettings.apply(this, arguments);
            try { await appendRolesSection(); } catch (e) { /* optional section */ }
            return result;
        };
    }

    const originalSaveSettings = window.saveProjectSettings;
    if (typeof originalSaveSettings === "function") {
        window.saveProjectSettings = function () {
            const roles = {};
            roleRows.forEach((role) => {
                const name = (role.name || "").trim();
                if (name && (role.username || "").trim()) {
                    roles[name] = { username: role.username.trim(), password: role.password };
                }
            });
            // Merge roles into the one save request the base function makes.
            const nativeFetch = window.fetch;
            window.fetch = function (url, options) {
                if (String(url).includes("/save-project-config") && options && options.body) {
                    window.fetch = nativeFetch;
                    try {
                        const payload = JSON.parse(options.body);
                        payload.roles = roles;
                        options = { ...options, body: JSON.stringify(payload) };
                    } catch (e) { /* leave the payload alone */ }
                }
                return nativeFetch.call(this, url, options);
            };
            try {
                return originalSaveSettings.apply(this, arguments);
            } finally {
                setTimeout(() => { window.fetch = nativeFetch; }, 3000);
            }
        };
    }

    /* ══ Keep the rules badge current ════════════════════════════════ */
    const originalLoad = window.loadSelectedFlow;
    if (typeof originalLoad === "function") {
        window.loadSelectedFlow = async function () {
            const result = await originalLoad.apply(this, arguments);
            try {
                const data = await api(`/get-rules?${flowQ()}`);
                setRulesBadge((data.project_rules || []).length + (data.flow_rules || []).length);
            } catch (e) { /* badge is optional */ }
            return result;
        };
    }
})();
