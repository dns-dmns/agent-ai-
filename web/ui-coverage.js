/* ═══════════════════════════════════════════════════════════════════════
   Coverage — scenarios, their test cases, and what is missing.

   A scenario is the objective; its test cases are the concrete data rows that
   cover it. This surface makes both countable: how many scenarios, how many
   test cases, which checklist behaviours the suite never touches, and a button
   that enumerates a field's permutations without calling the model at all.

   Like ui.js and ui-rules.js, this only wraps app.js — it never edits it.
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v === undefined || v === null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");

    const flowQ = () =>
        `project_name=${encodeURIComponent(window.activeProject())}` +
        `&flow_name=${encodeURIComponent(window.activeFlow())}`;

    async function api(url, options) {
        const response = await fetch(url, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
        return data;
    }

    /* ══ The coverage badge, kept current with the loaded flow ═══════ */
    function setCoverageBadge(summary) {
        const badge = $("coverageBadge");
        if (!badge) return;
        if (!summary) {
            badge.textContent = "—";
            badge.className = "status idle";
            return;
        }
        badge.textContent = `${summary.test_cases} cases · ${summary.checklist_percent}%`;
        badge.className = "status " + (summary.checklist_percent >= 90 ? "pass"
            : summary.checklist_percent >= 60 ? "warn" : "fail");
        badge.title = summary.gaps.length
            ? `Not covered: ${summary.gaps.map((g) => g.title).join(", ")}`
            : "Every checklist behaviour has at least one test case";
    }

    /* ══ Coverage report ════════════════════════════════════════════ */
    window.openCoverageReport = async function () {
        if (!window.activeFlow()) return window.toast("Load a module first", "error");
        let summary;
        try {
            summary = await api(`/coverage-report?${flowQ()}`);
        } catch (e) {
            return window.toast(e.message, "error");
        }

        const gapRows = summary.gaps.length
            ? summary.gaps.map((g) => `
                <tr><td>${esc(g.title)}</td><td><span class="pill">${esc(g.type)}</span></td></tr>`).join("")
            : `<tr><td colspan="2" class="muted">Every checklist behaviour is covered.</td></tr>`;

        window.openModal(`Coverage — ${window.activeFlow()}`, `
            <div class="stat-row">
                <div class="stat"><span class="stat-value">${summary.scenarios}</span>
                    <span class="stat-label">scenarios</span></div>
                <div class="stat"><span class="stat-value">${summary.test_cases}</span>
                    <span class="stat-label">test cases</span></div>
                <div class="stat"><span class="stat-value">${summary.checklist_covered}/${summary.checklist_total}</span>
                    <span class="stat-label">behaviours</span></div>
                <div class="stat"><span class="stat-value">${summary.fields_known}</span>
                    <span class="stat-label">fields known</span></div>
            </div>
            <p class="field-hint">${summary.parameterised_scenarios} scenario(s) carry a data
               table, so one objective covers several test cases. ${summary.fields_known
                ? "" : "No fields are known yet — record the module or run it once."}</p>
            <div class="field">
                <span class="field-label">Behaviours with no test case</span>
                <table class="data-table"><thead><tr><th>Behaviour</th><th>Type</th></tr></thead>
                    <tbody>${gapRows}</tbody></table>
            </div>
        `,
            `<button class="btn btn-primary" onclick="previewCoverageCases()">
                Enumerate field cases</button>
             <button class="btn" onclick="openPageCoverage()">Page coverage</button>
             <button class="btn" style="background:#334155" onclick="closeModal()">Close</button>`);
        setCoverageBadge(summary);
    };

    /* ══ Enumerated field cases ═════════════════════════════════════ */
    window.previewCoverageCases = async function () {
        if (!window.activeFlow()) return window.toast("Load a module first", "error");
        let data;
        try {
            window.toast("Enumerating the permutations for every field…", "info");
            data = await api(`/preview-coverage-cases?${flowQ()}`);
        } catch (e) {
            return window.toast(e.message, "error");
        }

        const blocks = data.scenarios.map((s) => `
            <details class="spec-block">
                <summary><strong>${esc(s.scenario_name)}</strong>
                    <span class="pill">${s.data_table.length} test cases</span></summary>
                <table class="data-table">
                    <thead><tr><th>Test case</th><th>Value</th><th>Expected</th></tr></thead>
                    <tbody>${s.data_table.map((r) => `
                        <tr><td>${esc(r.case_name)}</td>
                            <td class="mono-sm">${esc(String(r.values.value).slice(0, 40))}${
                                String(r.values.value).length > 40 ? "…" : ""}</td>
                            <td>${esc(r.expected_result)}</td></tr>`).join("")}
                    </tbody>
                </table>
            </details>`).join("");

        window.openModal("Enumerated test cases", `
            <p class="field-hint">Derived from each field's own type and constraints — the
               same form always yields the same set, and no API key is used. Submit control:
               <code>${esc(data.submit_selector)}</code>.
               ${data.skipped_fields.length ? `Skipped as sensitive:
               <code>${esc(data.skipped_fields.join(", "))}</code>.` : ""}</p>
            <div class="stat-row">
                <div class="stat"><span class="stat-value">${data.scenario_count}</span>
                    <span class="stat-label">scenarios</span></div>
                <div class="stat"><span class="stat-value">${data.test_case_count}</span>
                    <span class="stat-label">test cases</span></div>
            </div>
            ${blocks || '<p class="muted">No fields could be enumerated.</p>'}
        `,
            `<button class="btn btn-primary" onclick="addCoverageCases()">
                Add ${data.scenario_count} scenario(s) — ${data.test_case_count} test cases</button>
             <button class="btn" style="background:#334155" onclick="openCoverageReport()">Back</button>`);
    };

    window.addCoverageCases = async function () {
        try {
            const result = await api(`/add-coverage-cases?${flowQ()}`, { method: "POST" });
            window.toast(`Added ${result.added} scenario(s) — ${result.test_cases} test cases`,
                         "success");
            window.closeModal?.();
            await window.loadSelectedFlow?.();
        } catch (e) {
            window.toast(e.message, "error");
        }
    };

    /* ══ Page coverage: controls no test ever touches ═══════════════ */
    window.openPageCoverage = async function () {
        if (!window.activeFlow()) return window.toast("Load a module first", "error");
        let data;
        try {
            window.toast("Loading the page and enumerating every control…", "info");
            data = await api(`/page-coverage?${flowQ()}`);
        } catch (e) {
            return window.toast(e.message, "error");
        }

        const row = (c) => `
            <tr class="${c.verdict === "notable" ? "text-fail" : ""}">
                <td>${esc(c.verdict === "notable" ? "!" : "·")}</td>
                <td>${esc(c.tag)}${c.type ? `[${esc(c.type)}]` : ""}</td>
                <td>${esc(c.label || "(no label)")}</td>
                <td class="mono-sm">${esc(c.selector)}</td>
            </tr>`;

        const notable = data.uncovered.filter((c) => c.verdict === "notable");
        const rest = data.uncovered.filter((c) => c.verdict !== "notable");

        window.openModal(`Page coverage — ${window.activeFlow()}`, `
            <div class="stat-row">
                <div class="stat"><span class="stat-value">${data.total}</span>
                    <span class="stat-label">controls found</span></div>
                <div class="stat"><span class="stat-value">${data.exercised}</span>
                    <span class="stat-label">exercised</span></div>
                <div class="stat"><span class="stat-value">${data.uncovered.length}</span>
                    <span class="stat-label">never tested</span></div>
                <div class="stat"><span class="stat-value">${notable.length}</span>
                    <span class="stat-label">worth testing</span></div>
            </div>
            ${data.note ? `<p class="field-hint">${esc(data.note)}</p>` : ""}
            <p class="field-hint">A recording only shows what the tester touched. These
               controls exist on <code>${esc(data.target_url)}</code> and nothing in the
               suite refers to them.</p>
            ${notable.length ? `
            <div class="field">
                <span class="field-label text-fail">Never tested, and probably should be</span>
                <table class="data-table"><tbody>${notable.map(row).join("")}</tbody></table>
            </div>` : `<p class="muted">Nothing important looks untested.</p>`}
            ${rest.length ? `
            <details class="spec-block">
                <summary>${rest.length} more untested control(s)</summary>
                <table class="data-table"><tbody>${rest.map(row).join("")}</tbody></table>
            </details>` : ""}
        `,
            `<button class="btn" style="background:#334155" onclick="closeModal()">Close</button>`);
    };

    /* ══ Grounding: never let a fabricated suite pass silently ══════════
       app.js turns any non-OK response from /analyze-flow into the generic
       "AI analysis failed.", so the reason never reaches the screen — and it
       ignores the success message, so a warning that 100% of the selectors were
       invented would be invisible. Both matter too much to lose, and app.js is
       never edited, so this replaces the function using the same element ids.
       The only closure state the original touched was the current project and
       flow, which activeProject()/activeFlow() already expose. */
    window.triggerAIAnalysis = async function () {
        const project = window.activeProject();
        const flow = window.activeFlow();
        if (!flow) return window.toast("Load a module first", "error");

        const loader = $("analysisLoader");
        const list = $("suiteList");
        const button = $("analyzeBtn");
        const configBox = $("configBox");
        const types = Array.from(
            document.querySelectorAll('input[name="testType"]:checked')
        ).map((cb) => cb.value).join(",");

        if (loader) loader.style.display = "flex";
        if (list) list.style.display = "none";
        if (configBox) configBox.style.display = "none";
        if (button) button.disabled = true;

        try {
            const response = await fetch(
                `/analyze-flow?flow_name=${encodeURIComponent(flow)}` +
                `&project_name=${encodeURIComponent(project)}` +
                `&test_types=${encodeURIComponent(types)}`, { method: "POST" });
            const data = await response.json().catch(() => ({}));

            if (!response.ok) {
                // The grounding guard explains itself in several sentences —
                // show it in full rather than as a one-line toast.
                // split/join rather than a regex: the escape sequences in a
                // regex literal are one more thing to get wrong here.
                const detail = esc(data.detail || "AI analysis failed.")
                    .split("\n\n").join("<br><br>");
                window.openModal("Cannot generate from this recording", `
                    <div class="nudge"><span>!</span><span>${detail}</span></div>`,
                    `<button class="btn" style="background:#334155"
                        onclick="closeModal()">Close</button>`);
                return;
            }

            await window.loadSelectedFlow?.();
            const grounding = data.grounding;
            if (grounding && !grounding.grounded) {
                window.openModal("Generated, but mostly guesswork", `
                    <div class="nudge"><span>!</span><span>${esc(data.message)}</span></div>
                    <div class="stat-row">
                        <div class="stat"><span class="stat-value">${grounding.invented_percent}%</span>
                            <span class="stat-label">selectors invented</span></div>
                        <div class="stat"><span class="stat-value">${grounding.invented_count}/${grounding.selectors_used}</span>
                            <span class="stat-label">not in the recording</span></div>
                    </div>
                    <div class="field">
                        <span class="field-label">Selectors the agents guessed</span>
                        <table class="data-table"><tbody>${
                            grounding.invented.map((sel) =>
                                `<tr><td class="mono-sm">${esc(sel)}</td></tr>`).join("")
                        }</tbody></table>
                    </div>`,
                    `<button class="btn" style="background:#334155"
                        onclick="closeModal()">Close</button>`);
            } else {
                window.toast(data.message || "Specification generated", "success");
            }
        } catch (e) {
            window.toast(e.message, "error");
        } finally {
            if (loader) loader.style.display = "none";
            if (list) list.style.display = "flex";
            if (button) button.disabled = false;
        }
    };

    /* ══ Group the results panel by scenario after a run ════════════ */
    window.renderScenarioGroups = function (groups) {
        const host = $("scenarioGroups");
        if (!host) return;
        if (!groups || !groups.length) {
            host.innerHTML = "";
            return;
        }
        host.innerHTML = groups.map((g) => `
            <div class="group-row ${g.verdict.toLowerCase()}">
                <span class="group-name">${esc(g.scenario)}</span>
                <span class="group-summary">${esc(g.summary)}</span>
                ${g.failures.length
                    ? `<span class="group-failures" title="${esc(g.failures.join(", "))}">
                         failed: ${esc(g.failures.slice(0, 3).join(", "))}${
                           g.failures.length > 3 ? ` +${g.failures.length - 3}` : ""}</span>`
                    : ""}
            </div>`).join("");
    };

    /* ══ Refresh the badge whenever a module is loaded ═══════════════ */
    const originalLoad = window.loadSelectedFlow;
    if (typeof originalLoad === "function") {
        window.loadSelectedFlow = async function () {
            const result = await originalLoad.apply(this, arguments);
            try {
                setCoverageBadge(await api(`/coverage-report?${flowQ()}`));
            } catch (e) {
                setCoverageBadge(null);          // the badge is optional
            }
            return result;
        };
    }
})();

/* ══ Show the grouped results whenever a report is restored ══════════════
   app.js owns restoreExecutionReport and keeps its state in closure scope, so
   this wraps it rather than replacing it. The grouping is purely additive: the
   per-scenario cards app.js draws are untouched. */
(function () {
    "use strict";
    const original = window.restoreExecutionReport;
    if (typeof original !== "function") return;
    window.restoreExecutionReport = function (latestReport) {
        const result = original.apply(this, arguments);
        try {
            const groups = (latestReport && latestReport.scenario_groups) || [];
            // Only worth a panel when a scenario actually expanded into several
            // test cases; otherwise it just repeats the cards above it.
            const worthShowing = groups.filter((g) => g.total > 1).length > 0;
            const section = document.getElementById("scenarioGroupSection");
            if (section) section.classList.toggle("hidden", !worthShowing);
            window.renderScenarioGroups(worthShowing ? groups : []);
        } catch (e) { /* presentation only — never break the report */ }
        return result;
    };
})();
