/* ═══════════════════════════════════════════════════════════════════════
   Execution engine choice — platform verdicts vs the Playwright framework.

   The platform has two execution worlds. Its own engine produces evidence-based
   PASS/FAIL/UNVERIFIED/CRASH verdicts. The exported POM framework runs on stock
   @playwright/test, where a test passes when no assertion throws. This adapter
   adds that second engine to Run & Verify: same files the export produces,
   generated into the platform's scratch folder and driven by npx.

   Honesty rule carried into the UI: framework results are labelled "assertions
   only" everywhere — they must never read as verdicts.

   House rule: app.js is never edited. This wraps triggerTestSuite the same way
   ui.js wraps its functions.
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v === undefined || v === null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

    let frameworkSource = null;
    let frameworkRunId = "";

    /* ── The engine selector, injected next to Mode ─────────────────── */
    function injectEngineField() {
        const modeSelect = $("executionModeSelect");
        if (!modeSelect || $("engineSelect")) return;
        const field = document.createElement("div");
        field.className = "field";
        field.innerHTML =
            '<label class="field-label" for="engineSelect">Execution engine</label>' +
            '<select id="engineSelect">' +
            '<option value="platform" selected>Platform — evidence &amp; verdicts</option>' +
            '<option value="framework">Playwright framework — assertions only</option>' +
            '</select>' +
            '<span class="field-hint" id="engineHint" style="display:none">Runs the ' +
            'same @playwright/test project the export produces (needs Node.js). ' +
            'Pass/fail comes from assertions alone — no UNVERIFIED, no message ' +
            'scanning, no rules.</span>';
        modeSelect.closest(".field").insertAdjacentElement("afterend", field);
        field.querySelector("#engineSelect").addEventListener("change", (e) => {
            $("engineHint").style.display =
                e.target.value === "framework" ? "block" : "none";
        });
    }

    /* ── Terminal helpers (the ones app.js keeps in closure scope) ───── */
    function line(text, color) {
        const terminal = $("terminalConsole");
        if (!terminal) return;
        const row = document.createElement("div");
        if (color) row.style.color = color;
        row.textContent = text;
        terminal.appendChild(row);
        terminal.scrollTop = terminal.scrollHeight;
    }

    function renderFrameworkReport(report) {
        const card = $("reportCard");
        if (!card) return;
        const rows = (report.tests || []).map((t) => {
            const color = t.status === "PASS" ? "var(--pass, #22c55e)"
                : t.status === "FAIL" ? "var(--fail, #ef4444)" : "var(--text-muted)";
            const flaky = t.outcome === "flaky" ? " (flaky — passed on retry)" : "";
            return `<tr><td>${esc(t.name)}</td>` +
                `<td style="color:${color};font-weight:600">${esc(t.status)}${flaky}</td>` +
                `<td class="mono-sm">${(t.duration_ms / 1000).toFixed(1)}s</td>` +
                `<td class="mono-sm">${esc(t.error || "")}</td></tr>`;
        }).join("");
        card.style.display = "block";
        card.innerHTML = `
            <h3>🎭 Playwright framework run — assertions only</h3>
            <p class="field-hint">${esc(report.engine_note || "")}</p>
            <div class="stat-row">
                <div class="stat"><span class="stat-value">${report.passed_count}</span>
                    <span class="stat-label">passed</span></div>
                <div class="stat"><span class="stat-value">${report.failed_count}</span>
                    <span class="stat-label">failed</span></div>
                <div class="stat"><span class="stat-value">${report.skipped_count}</span>
                    <span class="stat-label">skipped</span></div>
                <div class="stat"><span class="stat-value">${report.duration_seconds}s</span>
                    <span class="stat-label">duration</span></div>
            </div>
            <div style="overflow-x:auto"><table class="data-table">
                <thead><tr><th>Test</th><th>Result</th><th>Time</th><th>Error</th></tr></thead>
                <tbody>${rows}</tbody></table></div>
            <p class="field-hint">Saved as <code>report.playwright.json</code> —
               kept separate from the verdict engine's report.json on purpose.</p>`;
    }

    function stopFrameworkRun() {
        if (frameworkRunId) {
            fetch(`/stop-run?run_id=${encodeURIComponent(frameworkRunId)}`,
                  { method: "POST" }).catch(() => {});
        }
        line("⏹ Stop requested — the framework process will be killed.", "#f59e0b");
    }

    function runFramework() {
        // app.js keeps currentProjectName/currentFlowName in closure scope;
        // these helpers (defined in ui-rules.js) are the reliable way in.
        const project = window.activeProject();
        const flow = window.activeFlow();
        if (!project) {
            return window.toast ? window.toast("Open a workspace first", "error")
                                : alert("Open a workspace first");
        }

        const terminal = $("terminalConsole");
        const report = $("reportCard");
        const startBtn = $("runSuiteBtn");
        const stopBtn = $("stopSuiteBtn");
        if (terminal) { terminal.innerHTML = ""; terminal.style.display = "block"; }
        if (report) report.style.display = "none";
        if (startBtn) startBtn.disabled = true;
        if (stopBtn) {
            stopBtn.style.display = "inline-block";
            stopBtn.classList.remove("hidden");
            stopBtn.disabled = false;
            stopBtn.onclick = stopFrameworkRun;      // restored on finish
        }

        const url = `/run-framework-stream?project_name=${encodeURIComponent(project)}` +
                    `&flow_name=${encodeURIComponent(flow)}`;
        const source = new EventSource(url);
        frameworkSource = source;

        const finish = () => {
            source.close();
            frameworkSource = null;
            frameworkRunId = "";
            if (startBtn) startBtn.disabled = false;
            if (stopBtn) { stopBtn.style.display = "none"; stopBtn.onclick = null; }
        };

        source.onmessage = (event) => {
            let data;
            try { data = JSON.parse(event.data); } catch (e) { return; }
            if (data.run_id) frameworkRunId = data.run_id;
            if (data.event === "framework_complete") {
                line(data.message, "#22c55e");
                renderFrameworkReport(data.report || {});
                finish();
            } else if (data.event === "error") {
                line(data.message, "#ef4444");
                finish();
            } else if (data.message) {
                line(data.message);
            }
        };
        source.onerror = () => {
            line("⚠️ Stream ended.", "#f59e0b");
            finish();
        };
    }

    /* ── Route the Run button through the selected engine ────────────── */
    const originalTrigger = window.triggerTestSuite;
    window.triggerTestSuite = function (selectedOnly = false) {
        const engine = $("engineSelect");
        if (engine && engine.value === "framework") {
            if (selectedOnly) {
                return window.toast
                    ? window.toast("Selected-only runs need the Platform engine — "
                                   + "the framework runs whole spec files.", "error")
                    : alert("Selected-only runs need the Platform engine.");
            }
            return runFramework();
        }
        return originalTrigger.apply(this, arguments);
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", injectEngineField);
    } else {
        injectEngineField();
    }
})();
