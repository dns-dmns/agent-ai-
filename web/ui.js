/* ═══════════════════════════════════════════════════════════════════════
   UI layer — loaded after app.js.

   app.js holds the behaviour and is kept byte-identical to the original
   single-file dashboard. This file only changes presentation:

     · bridges the redesigned sidebar to the <select> elements the logic reads
     · drives the workflow rail, theme, command palette and toasts
     · replaces three render functions (scenarios, timeline, status) with
       redesigned markup, keeping every element id and class those functions
       are contractually required to produce

   Nothing here reaches into app.js internals except by wrapping its public
   functions, so the logic can be updated independently.
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    /* ── Small helpers ──────────────────────────────────────────────── */
    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v === undefined || v === null ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");

    const STATUS = {
        PASS:       { key: "pass",       glyph: "✓", label: "Passed" },
        FAIL:       { key: "fail",       glyph: "✕", label: "Failed" },
        UNVERIFIED: { key: "unverified", glyph: "!", label: "Unverified" },
        CRASH:      { key: "crash",      glyph: "✻", label: "Crashed" },
        CANCELLED:  { key: "idle",       glyph: "■", label: "Cancelled" },
        RUNNING:    { key: "running",    glyph: "•", label: "Running" },
    };
    const statusOf = (s) => STATUS[String(s || "").toUpperCase()] ||
        { key: "idle", glyph: "·", label: "Not run" };

    /* ── Appearance ─────────────────────────────────────────────────── */
    const THEME_KEY = "qa.theme";

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        const btn = $("themeToggle");
        if (btn) {
            btn.textContent = theme === "dark" ? "◐" : "◑";
            btn.title = theme === "dark" ? "Switch to light appearance"
                                         : "Switch to dark appearance";
        }
    }

    window.toggleTheme = function () {
        const next = document.documentElement.getAttribute("data-theme") === "dark"
            ? "light" : "dark";
        localStorage.setItem(THEME_KEY, next);
        applyTheme(next);
    };

    applyTheme(localStorage.getItem(THEME_KEY) ||
        (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));

    window.toggleSidebar = () => $("sidebar").classList.toggle("open");

    /* ── Toasts: feedback that doesn't block the flow ───────────────── */
    window.toast = function (message, kind = "") {
        const host = $("toasts");
        if (!host) return;
        const el = document.createElement("div");
        el.className = `toast ${kind ? "is-" + kind : ""}`;
        el.innerHTML = `<span>${kind === "error" ? "✕" : kind === "ok" ? "✓" : "›"}</span>
                        <span>${esc(message)}</span>`;
        host.appendChild(el);
        setTimeout(() => {
            el.style.opacity = "0";
            el.style.transform = "translateX(12px)";
            setTimeout(() => el.remove(), 240);
        }, 4200);
    };

    // Route the logic's alert() calls into toasts — same information, no modal wall.
    const nativeAlert = window.alert.bind(window);
    window.alert = function (message) {
        const text = String(message);
        if (text.length > 400) return nativeAlert(message);
        const looksBad = /error|failed|cannot|invalid|required|missing/i.test(text);
        window.toast(text, looksBad ? "error" : "ok");
    };

    /* ── Sidebar ⇄ <select> bridge ──────────────────────────────────── */
    function flowStatusFromCache(name) {
        const cached = window.__flowStatus && window.__flowStatus[name];
        return cached || "none";
    }

    /* Flows as a folder tree. A flow saved as "Master/Item Master" (a folder on
       disk: projects/<ws>/flows/Master/Item Master) shows under a "Master"
       folder, mirroring the application's own menu. Folders fold; the folder
       holding the selected flow always opens. */
    const FOLD_KEY = "qa.folders.closed";
    const closedFolders = (() => {
        try { return new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || "[]")); }
        catch (e) { return new Set(); }
    })();
    const saveFolders = () => {
        try { localStorage.setItem(FOLD_KEY, JSON.stringify([...closedFolders])); } catch (e) {}
    };

    function flowTree(names) {
        const root = { folders: new Map(), flows: [] };
        names.forEach((full) => {
            const parts = full.split("/");
            let node = root;
            parts.slice(0, -1).forEach((part) => {
                if (!node.folders.has(part)) node.folders.set(part, { folders: new Map(), flows: [] });
                node = node.folders.get(part);
            });
            node.flows.push({ full, label: parts[parts.length - 1] });
        });
        return root;
    }

    const countFlows = (node) =>
        node.flows.length + [...node.folders.values()].reduce((n, f) => n + countFlows(f), 0);

    function treeHtml(node, path, depth, current, scope) {
        const pad = `style="padding-left:calc(var(--space-3) + ${depth} * 14px)"`;
        let html = "";
        [...node.folders.keys()].sort((a, b) => a.localeCompare(b)).forEach((name) => {
            const child = node.folders.get(name);
            const key = `${scope}:${path}${name}`;
            const holdsCurrent = current && current.startsWith(`${path}${name}/`);
            const open = holdsCurrent || !closedFolders.has(key);
            html += `<button class="row flow-folder" ${pad} aria-expanded="${open}"
                        onclick="toggleFlowFolder('${esc(key).replace(/'/g, "\'")}')">
                        <span class="row-icon">${open ? "▾" : "▸"}</span>
                        <span class="row-text">${esc(name)}</span>
                        <span class="row-meta">${countFlows(child)}</span>
                     </button>
                     <div class="flow-folder-children" ${open ? "" : "hidden"}>
                        ${treeHtml(child, `${path}${name}/`, depth + 1, current, scope)}
                     </div>`;
        });
        node.flows.sort((a, b) => a.label.localeCompare(b.label)).forEach(({ full, label }) => {
            const active = full === current;
            html += `<button class="row ${active ? "is-active" : ""}" ${pad} title="${esc(full)}"
                        aria-selected="${active}" data-flow="${esc(full)}"
                        onclick="selectFlowFromSidebar('${esc(full).replace(/'/g, "\'")}')">
                        <span class="dot is-${flowStatusFromCache(full)}"></span>
                        <span class="row-text">${esc(label)}</span>
                     </button>`;
        });
        return html;
    }

    function renderSidebarFlows() {
        const host = $("sidebarFlowList");
        const select = $("flowSelect");
        if (!host || !select) return;

        const groups = [
            { label: "Authentication", group: $("authFlowsGroup") },
            { label: "Business", group: $("businessFlowsGroup") },
        ];
        const current = select.value;
        let html = "";
        let total = 0;

        groups.forEach(({ label, group }) => {
            const names = group ? [...group.querySelectorAll("option")].map((o) => o.value) : [];
            if (!names.length) return;
            total += names.length;
            html += `<div class="sidebar-label" style="padding-top:var(--space-3)">
                        <span>${label}</span><span class="row-meta">${names.length}</span>
                     </div>`;
            html += treeHtml(flowTree(names), "", 0, current, label);
        });

        host.innerHTML = total ? html : `
            <div class="empty" style="padding:var(--space-6) var(--space-3)">
                <div class="empty-text dim">No flows yet. Record one to get started.</div>
            </div>`;
    }

    window.toggleFlowFolder = function (key) {
        if (closedFolders.has(key)) closedFolders.delete(key); else closedFolders.add(key);
        saveFolders();
        renderSidebarFlows();
    };

    /* The tools list at the bottom folds away so it never covers the flows. */
    window.toggleSidebarTools = function (force) {
        const box = $("sidebarTools");
        if (!box) return;
        const fold = typeof force === "boolean" ? force : !box.classList.contains("is-folded");
        box.classList.toggle("is-folded", fold);
        // Set directly as well: folding must work even if an old stylesheet is cached.
        const list = $("sidebarToolsList");
        if (list) list.style.display = fold ? "none" : "";
        const btn = $("sidebarToolsToggle"), chevron = $("sidebarToolsChevron");
        if (btn) btn.setAttribute("aria-expanded", String(!fold));
        if (chevron) chevron.textContent = fold ? "▸" : "▾";
        try { localStorage.setItem("qa.tools.folded", fold ? "1" : "0"); } catch (e) {}
    };
    (() => {
        let folded = true;   // folded by default: the flow list gets the room
        try { folded = localStorage.getItem("qa.tools.folded") !== "0"; } catch (e) {}
        window.toggleSidebarTools(folded);
    })();

    window.selectFlowFromSidebar = function (name) {
        const select = $("flowSelect");
        if (!select) return;
        select.value = name;
        if (window.innerWidth <= 900) $("sidebar").classList.remove("open");
        window.loadSelectedFlow();
    };

    window.openProjectPicker = function () {
        const select = $("projectSelect");
        const options = [...select.options].map((o) => o.value).filter(Boolean);
        window.openModal("Switch workspace", `
            <div class="stack-sm">
                ${options.map((name) => `
                    <button class="row ${name === select.value ? "is-active" : ""}"
                        onclick="pickProject('${esc(name).replace(/'/g, "\\'")}')">
                        <span class="row-icon">◈</span><span class="row-text">${esc(name)}</span>
                        ${name === select.value ? '<span class="row-meta">current</span>' : ""}
                    </button>`).join("")}
            </div>
            <div class="divider">or</div>
            <div class="row-flex">
                <input type="text" id="newProjectInline" placeholder="New workspace name" style="flex:1">
                <button class="btn" onclick="createProjectInline()">Create</button>
            </div>`);
    };

    window.pickProject = function (name) {
        const select = $("projectSelect");
        select.value = name;
        window.closeModal();
        window.switchProject();
    };

    window.createProjectInline = async function () {
        const name = $("newProjectInline").value.trim();
        if (!name) return;
        const response = await fetch(`/create-project?project_name=${encodeURIComponent(name)}`,
            { method: "POST" });
        const data = await response.json();
        if (!response.ok) return window.toast(data.detail || "Could not create it", "error");
        window.closeModal();
        await window.fetchProjects(name);
        window.toast(`Workspace “${name}” created`, "ok");
    };

    /* ── Breadcrumb + rail state ────────────────────────────────────── */
    function syncChrome() {
        const project = $("projectSelect")?.value || "—";
        const flow = $("flowSelect")?.value || "";
        if ($("crumbProject")) $("crumbProject").textContent = project;
        if ($("crumbFlow")) {
            $("crumbFlow").textContent = flow || "No flow selected";
            $("crumbFlow").style.color = flow ? "" : "var(--text-tertiary)";
        }
        if ($("projectSwitcherName")) $("projectSwitcherName").textContent = project;
        document.title = flow ? `${flow} — QA Platform` : "QA Platform";

        // Mark completed workflow steps so the rail reads as progress.
        const steps = [...document.querySelectorAll(".rail-step")];
        const hasFlow = Boolean(flow);
        const hasCases = Boolean(window.loadedTestCases && window.loadedTestCases.length);
        const hasReport = $("reportStatusBadge") &&
            !["—", ""].includes($("reportStatusBadge").textContent.trim());
        [hasFlow, hasCases, hasReport].forEach((done, i) => {
            if (steps[i]) steps[i].classList.toggle("done", Boolean(done));
        });
    }

    /* ── Wrap the logic's public functions (never edit app.js) ──────── */
    function wrap(name, after) {
        const original = window[name];
        if (typeof original !== "function") return;
        window[name] = function (...args) {
            const result = original.apply(this, args);
            if (result && typeof result.then === "function") {
                return result.then((value) => { try { after(args, value); } catch (e) {} return value; });
            }
            try { after(args, result); } catch (e) {}
            return result;
        };
    }

    wrap("fetchSavedFlows", () => { renderSidebarFlows(); syncChrome(); });
    wrap("fetchProjects", () => { syncChrome(); syncExplorer(); });
    wrap("switchProject", () => { syncChrome(); syncExplorer(); });

    function syncExplorer() {
        // The suite explorer used to require picking a workspace by hand even
        // though one is already active. Keep it in step with the sidebar.
        const explorer = $("explorerProjectSelect");
        const active = $("projectSelect")?.value;
        if (!explorer || !active || explorer.value === active) return;
        if ([...explorer.options].some((o) => o.value === active)) {
            explorer.value = active;
            if (typeof window.loadExplorerFlowsList === "function") window.loadExplorerFlowsList();
        }
    }
    wrap("loadSelectedFlow", () => {
        renderSidebarFlows();
        syncChrome();
        cacheFlowStatus();
        // When a flow has no suite yet, the logic writes its own placeholder
        // straight into #suiteList. Repaint it as a designed empty state.
        const host = $("suiteList");
        if (host && !host.querySelector(".scenario")) paintSuiteList([]);
    });
    wrap("renderFinalReport", () => { syncChrome(); goto("report-view"); });
    wrap("restoreExecutionReport", () => syncChrome());
    wrap("switchMainTab", () => window.scrollTo({ top: 0 }));

    function cacheFlowStatus() {
        // Colour the sidebar dot from the loaded report, so state is visible
        // without opening each flow.
        const flow = $("flowSelect")?.value;
        const badge = $("reportStatusBadge")?.textContent?.trim();
        if (!flow || !badge) return;
        window.__flowStatus = window.__flowStatus || {};
        window.__flowStatus[flow] =
            badge === "PASS" ? "pass" : badge === "FAIL" ? "fail"
            : badge === "UNVERIFIED" ? "unverified" : "none";
        renderSidebarFlows();
    }

    /* ── Redesigned: platform status pills ──────────────────────────── */
    window.refreshPlatformStatus = async function () {
        try {
            const response = await fetch("/platform-info", { cache: "no-store" });
            if (response.status === 401) return window.promptForApiKey();
            const info = await response.json();
            const tokens = (info.llm_usage.approx_prompt_tokens +
                            info.llm_usage.approx_response_tokens) || 0;
            const host = $("platformStatus");
            if (!host) return;
            host.innerHTML = `
                <span class="pill ${info.ai_configured ? "is-ok" : "is-warn"}"
                      title="${info.ai_configured ? "Model " + esc(info.model)
                                                  : "Add GEMINI_API_KEY to .env to enable generation"}">
                    <span class="pill-dot"></span>${info.ai_configured ? "AI ready" : "No API key"}
                </span>
                <span class="pill" title="Approximate tokens used across recent AI calls">
                    ${tokens.toLocaleString()} tok
                </span>
                <span class="pill" title="Workspace folder">${esc(info.workspace)}</span>`;
        } catch (e) { /* status is best effort */ }
    };

    /* ── Redesigned: scenario list ──────────────────────────────────────
       Wrapped, not replaced: app.js keeps its own `loadedTestCases` binding
       (declared with `let`, so it is not reachable on `window`) and the editor,
       filter and selection helpers keep working against it. We simply repaint
       the markup afterwards. */
    function paintSuiteList(cases) {
        const list = cases || [];
        window.loadedTestCases = list;   // for the palette and nudges only
        const host = $("suiteList");
        if (!host) return;

        const hasAssertion = (tc) => (tc.steps || []).some((s) => {
            const a = typeof s === "string" ? s : (s.action || "");
            return /^(VERIFY|ASSERT)/i.test(String(a));
        });
        const unprovable = list.filter((tc) => !hasAssertion(tc)).length;
        const types = [...new Set(list.map((tc) => tc.type).filter(Boolean))].sort();

        if (!list.length) {
            host.innerHTML = `
                <div class="empty">
                    <div class="empty-icon">◇</div>
                    <div class="empty-title">No scenarios yet</div>
                    <div class="empty-text">Generate a suite from the recording, or import a JSON
                        suite you already have.</div>
                    <button class="btn btn-primary" onclick="switchMainTab('runner-view',
                        document.querySelectorAll('.rail-step')[1])">Go to Generate</button>
                </div>`;
            syncChrome();
            return;
        }

        host.innerHTML = `
            <div class="suite-toolbar">
                <span class="suite-count">${list.length} scenario${list.length === 1 ? "" : "s"}</span>
                <span class="spacer"></span>
                <input class="suite-search" id="suiteSearchInput" type="search"
                       placeholder="Filter scenarios…" oninput="applySuiteFilter()">
                <select id="suiteTypeFilter" onchange="applySuiteFilter()" style="width:auto">
                    <option value="">All types</option>
                    ${types.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("")}
                </select>
                <button class="btn btn-sm" onclick="toggleSelectAllCases()">Select all</button>
                <button class="btn btn-sm" onclick="openCaseEditor(0)">+ Scenario</button>
            </div>
            ${unprovable ? `
            <div style="padding:var(--space-4) var(--space-5); border-bottom:1px solid var(--border)">
                <div class="nudge">
                    <span>!</span>
                    <span><strong>${unprovable}</strong> scenario${unprovable === 1 ? " has" : "s have"}
                        no VERIFY step, so ${unprovable === 1 ? "it" : "they"} can only ever be reported
                        as unverified.</span>
                </div>
            </div>` : ""}
            <div id="suiteFilterInfo" class="dim mono-sm"
                 style="padding:0 var(--space-5); display:none"></div>
            <div id="suiteRows"></div>`;

        const rows = $("suiteRows");
        list.forEach((tc, index) => {
            const n = index + 1;
            const name = tc.scenario_name || tc.scenario || `Scenario ${n}`;
            const expected = tc.expected_result || tc.expected || "";
            const card = document.createElement("div");
            card.className = "scenario test-case-card";
            card.id = `suiteCard-${n}`;
            card.dataset.index = n;
            card.dataset.type = tc.type || "";

            const steps = (tc.steps || []).map((s) => {
                if (typeof s === "string") {
                    return `<div class="step"><span class="step-verb">STEP</span>
                            <span>${esc(s)}</span></div>`;
                }
                const verb = String(s.action || "");
                const isAssert = /^(VERIFY|ASSERT)/i.test(verb);
                const detail = [s.selector ? `<code>${esc(s.selector)}</code>` : "",
                                s.value ? `“${esc(s.value)}”` : ""].filter(Boolean).join(" ");
                return `<div class="step ${isAssert ? "is-assert" : ""}">
                            <span class="step-verb">${esc(verb)}</span><span>${detail}</span>
                        </div>`;
            }).join("");

            card.innerHTML = `
                <input type="checkbox" class="caseSelect" value="${n}"
                       onchange="updateSelectionUi()" aria-label="Select scenario ${n}">
                <span class="glyph idle" id="suiteCardGlyph-${n}">·</span>
                <div class="scenario-main">
                    <div class="scenario-title">
                        <span class="scenario-num">${n}</span>
                        <span>${esc(name)}</span>
                        <span class="tag">${esc(tc.type || "—")}</span>
                    </div>
                    ${expected ? `<div class="scenario-expected">Expects: ${esc(expected)}</div>` : ""}
                    <details class="stack-sm">
                        <summary class="dim mono-sm" style="cursor:pointer">
                            ${(tc.steps || []).length} steps</summary>
                        <div class="steps" style="margin-top:var(--space-2)">${steps}</div>
                    </details>
                    <div class="scenario-status" id="suiteCardStatus-${n}">Not run</div>
                    <div class="scenario-verdict" id="suiteCardVerdict-${n}"></div>
                </div>
                <div class="scenario-actions">
                    <button class="btn btn-ghost btn-sm" onclick="runSingleCase(${n})"
                            title="Run only this scenario">▶</button>
                    <button class="btn btn-ghost btn-sm" onclick="openCaseEditor(${n})"
                            title="Edit scenario">✎</button>
                    <button class="btn btn-ghost btn-sm" onclick="deleteCase(${n})"
                            title="Delete scenario">🗑</button>
                </div>`;
            rows.appendChild(card);
        });

        window.updateSelectionUi();
        syncChrome();
    }

    wrap("renderSuiteList", (args) => paintSuiteList(args[0]));

    // The filter info element is hidden until it has something to say.
    const originalFilter = window.applySuiteFilter;
    window.applySuiteFilter = function () {
        originalFilter.apply(this, arguments);
        const info = $("suiteFilterInfo");
        if (info) info.style.display = info.textContent.trim() ? "block" : "none";
    };

    /* ── Redesigned: recorded-step timeline ─────────────────────────── */
    const NO_SHOT = "data:image/svg+xml;utf8," + encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
           <rect width="320" height="200" fill="#12151f"/>
           <text x="160" y="105" fill="#4a5468" font-family="sans-serif" font-size="13"
                 text-anchor="middle">no screenshot</text></svg>`);

    function paintTimeline(actions) {
        const host = $("timelineContainer");
        if (!host) return;
        const list = actions || [];
        if (!list.length) {
            host.innerHTML = `
                <div class="empty" style="grid-column:1/-1">
                    <div class="empty-icon">◉</div>
                    <div class="empty-title">Nothing recorded yet</div>
                    <div class="empty-text">Launch the browser, press Start recording, then use the
                        application as a tester would.</div>
                </div>`;
            return;
        }
        host.innerHTML = list.map((act, i) => {
            const verb = String(act.action || "STEP").toUpperCase();
            const label = act.value ? `“${esc(act.value)}”`
                : esc(act.text || act.label || act.selector || "");
            return `<figure class="tl-step" style="margin:0">
                        <img class="tl-shot" loading="lazy" alt="Step ${i + 1}"
                             src="${esc(act.screenshot || NO_SHOT)}"
                             onerror="this.src='${NO_SHOT}'"
                             onclick="openLightbox('${esc(act.screenshot || "")}')">
                        <figcaption class="tl-meta">
                            <span class="tl-index">Step ${i + 1} · ${esc(verb)}</span>
                            <span class="tl-label">${label}</span>
                        </figcaption>
                    </figure>`;
        }).join("");
    }

    wrap("renderTimeline", (args) => paintTimeline(args[0]));

    /* ── Live status on scenario rows ───────────────────────────────── */
    function paintRow(index, status) {
        const info = statusOf(status);
        const glyph = $(`suiteCardGlyph-${index}`);
        if (glyph) {
            glyph.className = `glyph ${info.key}`;
            glyph.textContent = info.glyph;
        }
        const statusEl = $(`suiteCardStatus-${index}`);
        if (statusEl) statusEl.style.color = `var(--${info.key === "idle" ? "text-tertiary" : info.key})`;
        const verdictEl = $(`suiteCardVerdict-${index}`);
        if (verdictEl) verdictEl.style.color = `var(--${info.key === "idle" ? "text-secondary" : info.key})`;
    }

    // Observe the status lines the logic writes and mirror them onto the glyph,
    // so the redesign stays in sync without touching app.js.
    const observer = new MutationObserver((mutations) => {
        mutations.forEach((m) => {
            const target = m.target.nodeType === 1 ? m.target : m.target.parentElement;
            const el = target && target.closest ? target.closest('[id^="suiteCardStatus-"]') : null;
            if (!el) return;
            const index = el.id.split("-")[1];
            const text = el.textContent || "";
            const match = text.match(/PASS|FAIL|UNVERIFIED|CRASH|CANCELLED|EXECUTING/i);
            const raw = match ? match[0].toUpperCase() : "";
            paintRow(index, raw === "EXECUTING" ? "RUNNING" : raw);
            if (raw === "UNVERIFIED") addAssertionNudge(index);
        });
    });
    observer.observe(document.body, { childList: true, characterData: true, subtree: true });

    function addAssertionNudge(index) {
        const card = $(`suiteCard-${index}`);
        if (!card || card.querySelector(".nudge")) return;
        const nudge = document.createElement("div");
        nudge.className = "nudge";
        nudge.innerHTML = `<span>!</span>
            <span>Nothing proved this outcome. Add a VERIFY step to make it provable.</span>
            <button class="btn btn-sm nudge-action" onclick="openCaseEditor(${index})">
                Add assertion</button>`;
        card.querySelector(".scenario-main").appendChild(nudge);
    }

    /* ── Report badge as a semantic status chip ─────────────────────── */
    const badgeObserver = new MutationObserver(() => {
        const badge = $("reportStatusBadge");
        if (!badge) return;
        const info = statusOf(badge.textContent.trim());
        const next = `status ${info.key}`;
        if (badge.className !== next) badge.className = next;
    });
    if ($("reportStatusBadge")) {
        badgeObserver.observe($("reportStatusBadge"),
            { childList: true, characterData: true, subtree: true });
    }

    /* ── Command palette ────────────────────────────────────────────── */
    const COMMANDS = [
        { label: "Run the suite", hint: "verify", run: () => window.triggerTestSuite() },
        { label: "Run selected scenarios", hint: "verify", run: () => window.triggerTestSuite(true) },
        { label: "Stop the run", hint: "verify", run: () => window.stopExecution() },
        { label: "Multi-agent generate", hint: "generate", run: () => window.triggerMultiAgentGeneration() },
        { label: "Generate QA specification", hint: "generate", run: () => window.triggerAIAnalysis() },
        { label: "Validate selectors", hint: "verify", run: () => window.triggerPreflight() },
        { label: "Run history & compare", hint: "report", run: () => window.openRunHistory() },
        { label: "Flaky scenarios", hint: "report", run: () => window.openFlakyReport() },
        { label: "Printable report", hint: "report", run: () => window.openPrintableReport() },
        { label: "Download evidence ZIP", hint: "report", run: () => window.downloadEvidenceBundle() },
        { label: "Project settings", hint: "workspace", run: () => window.openProjectSettings() },
        { label: "Schedules", hint: "workspace", run: () => window.openScheduleManager() },
        { label: "Add a scenario", hint: "edit", run: () => window.openCaseEditor(0) },
        { label: "Switch workspace", hint: "workspace", run: () => window.openProjectPicker() },
        { label: "Toggle appearance", hint: "view", run: () => window.toggleTheme() },
        { label: "Go to Record", hint: "navigate", run: () => goto("recorder-view") },
        { label: "Go to Generate", hint: "navigate", run: () => goto("runner-view") },
        { label: "Go to Run & verify", hint: "navigate", run: () => goto("verify-view") },
        { label: "Go to Report", hint: "navigate", run: () => goto("report-view") },
        { label: "Go to Console", hint: "navigate", run: () => goto("project-view") },
    ];

    let paletteItems = [];
    let paletteIndex = 0;

    function goto(viewId) {
        const map = {
            "recorder-view": 0, "runner-view": 1, "verify-view": 2,
            "report-view": 3, "project-view": 4,
        };
        const steps = document.querySelectorAll(".rail-step");
        window.switchMainTab(viewId, steps[map[viewId]] || steps[0]);
        if (viewId === "project-view") window.loadProjectConsoleStats();
    }
    window.gotoStep = goto;

    window.openPalette = function () {
        $("paletteScrim").classList.add("open");
        const input = $("paletteInput");
        input.value = "";
        window.renderPalette();
        setTimeout(() => input.focus(), 40);
    };
    window.closePalette = () => $("paletteScrim").classList.remove("open");

    window.renderPalette = function () {
        const term = ($("paletteInput").value || "").toLowerCase();
        const flows = [...($("flowSelect")?.querySelectorAll("option") || [])]
            .map((o) => o.value).filter(Boolean)
            .map((name) => ({
                label: name, hint: "flow",
                run: () => window.selectFlowFromSidebar(name),
            }));

        paletteItems = [...flows, ...COMMANDS]
            .filter((item) => !term || item.label.toLowerCase().includes(term) ||
                              item.hint.includes(term))
            .slice(0, 40);
        paletteIndex = 0;

        const host = $("paletteList");
        host.innerHTML = paletteItems.length
            ? paletteItems.map((item, i) => `
                <button class="row ${i === 0 ? "is-active" : ""}" data-i="${i}"
                        onclick="runPaletteItem(${i})">
                    <span class="row-icon">${item.hint === "flow" ? "▸" : "⌘"}</span>
                    <span class="row-text">${esc(item.label)}</span>
                    <span class="row-meta">${esc(item.hint)}</span>
                </button>`).join("")
            : `<div class="palette-empty">Nothing matches “${esc(term)}”</div>`;
    };

    window.runPaletteItem = function (i) {
        const item = paletteItems[i];
        window.closePalette();
        if (item) setTimeout(() => item.run(), 60);
    };

    window.paletteKeydown = function (event) {
        if (event.key === "Escape") return window.closePalette();
        if (event.key === "Enter") return window.runPaletteItem(paletteIndex);
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
        event.preventDefault();
        paletteIndex = Math.max(0, Math.min(paletteItems.length - 1,
            paletteIndex + (event.key === "ArrowDown" ? 1 : -1)));
        [...$("paletteList").children].forEach((el, i) =>
            el.classList.toggle("is-active", i === paletteIndex));
        $("paletteList").children[paletteIndex]?.scrollIntoView({ block: "nearest" });
    };

    /* ── Keyboard shortcuts ─────────────────────────────────────────── */
    document.addEventListener("keydown", (event) => {
        const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
            event.preventDefault();
            return window.openPalette();
        }
        if (event.key === "Escape") {
            if ($("paletteScrim").classList.contains("open")) return window.closePalette();
            if ($("genericModal").classList.contains("open")) return window.closeModal();
            if ($("lightbox").classList.contains("open")) return window.closeLightbox();
            if ($("videoModal").classList.contains("open")) return window.closeVideoModal();
        }
        if (inField) return;
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
            event.preventDefault();
            return window.triggerTestSuite();
        }
        if (["1", "2", "3", "4", "5"].includes(event.key)) {
            const views = ["recorder-view", "runner-view", "verify-view",
                           "report-view", "project-view"];
            goto(views[Number(event.key) - 1]);
        }
    });

    /* ── Overlay class bridge (the logic sets style.display) ────────── */
    ["genericModal", "lightbox", "videoModal", "paletteScrim"].forEach((id) => {
        const el = $(id);
        if (!el) return;
        new MutationObserver(() => {
            const inline = el.style.display;
            if (inline && inline !== "none") { el.style.display = ""; el.classList.add("open"); }
            else if (inline === "none") { el.classList.remove("open"); }
        }).observe(el, { attributes: true, attributeFilter: ["style"] });
    });

    /* ── Codegen-style assert mode ──────────────────────────────────── */
    // Playwright codegen's "assert text" tool. Switch it on, click through the
    // screen, and every click records a check on that element's exact text
    // instead of driving the application — so a spelling change fails the run.
    //
    // Lives here rather than in app.js because that file is frozen; the button
    // is created against the existing markup and talks to /set-record-mode
    // directly, so no app.js state is involved.
    let assertModeOn = false;

    function paintAssertButton() {
        const btn = $("assertModeBtn");
        if (!btn) return;
        btn.classList.toggle("btn-primary", assertModeOn);
        btn.textContent = assertModeOn ? "◉ Capturing checks" : "◎ Capture checks";
        btn.title = assertModeOn
            ? "ON — clicking an element records a check on its text. Click again to resume normal recording."
            : "Record checks on what the screen says (spelling, labels, values) instead of clicking through it.";
    }

    window.toggleAssertMode = async () => {
        const next = !assertModeOn;
        try {
            const res = await fetch(`/set-record-mode?assert_mode=${next}`, { method: "POST" });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "could not switch mode");
            assertModeOn = data.assert_mode;
            paintAssertButton();
            const line = $("recorderConsole");
            if (line) line.textContent = data.message;
        } catch (e) {
            const line = $("recorderConsole");
            if (line) line.textContent = `Could not switch mode: ${e.message}`;
        }
    };

    // Enable alongside Start recording, reset on Stop.
    wrap("triggerStartRecording", () => {
        const btn = $("assertModeBtn");
        if (btn) btn.disabled = false;
    });
    wrap("triggerStopRecording", () => {
        assertModeOn = false;
        paintAssertButton();
        const btn = $("assertModeBtn");
        if (btn) btn.disabled = true;
    });

    /* ── Misc small handlers used by the new markup ─────────────────── */
    window.toggleUploadPanel = () => $("uploadCustomSuitePanel").classList.toggle("hidden");
    window.showChosenFile = (input) => {
        $("uploadFileNameLabel").textContent = input.files[0] ? input.files[0].name : "No file chosen";
    };
    window.pickDataMatrix = () => $("dataMatrixInput").click();
    window.clearTerminal = () => { $("terminalConsole").innerHTML = ""; };

    /* ── Boot ───────────────────────────────────────────────────────── */
    document.addEventListener("DOMContentLoaded", () => {
        renderSidebarFlows();
        syncChrome();
        paintTimeline([]);
        paintSuiteList([]);
        // app.js already called its own version at load, before this file
        // replaced it — repaint so the redesigned pills are what you see.
        window.refreshPlatformStatus();
    });
    // app.js may already have run by the time we get here.
    renderSidebarFlows();
    syncChrome();
    window.refreshPlatformStatus();
    if ($("timelineContainer") && !$("timelineContainer").children.length) paintTimeline([]);
    if ($("suiteList") && !$("suiteList").children.length) paintSuiteList([]);
})();
