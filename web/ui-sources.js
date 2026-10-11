/* Test source: label every scenario with where it came from, and let a run pick.

   A suite holds cases from several writers -- Claude, Gemini's agents, an
   uploaded sheet, coverage enumeration, the defect log. The backend tags each
   with `_generated` (app/sources.py). This file adds, without editing app.js:

   - a coloured source badge on every scenario card,
   - a "Test source" filter beside "All types", with a count per source,
   - the chosen source on the run request (`&sources=`), so Run suite executes
     only those cases. The server filter is strict: choosing Claude never falls
     back to running everything.
   - the Test speed choice (`&speed=`, see app/pacing.py), appended the same way.
*/
(function () {
    "use strict";

    const LABELS = {
        claude: "Claude", gemini: "Gemini (API)", user: "Excel / CSV",
        coverage: "Coverage", defect: "Defect log", rule: "Rules",
        relationship: "Dependencies", duplicate: "Duplicate checks",
        manual: "Manual / older",
    };
    let shownCases = [];

    const sourceOf = (tc) => {
        const tag = String((tc && tc._generated) || "").toLowerCase();
        return LABELS[tag] ? tag : "manual";
    };
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const selected = () => (document.getElementById("suiteSourceFilter") || {}).value || "";

    function decorate(cases) {
        shownCases = Array.isArray(cases) ? cases : [];
        const toolbar = document.querySelector(".suite-toolbar");
        const typeFilter = document.getElementById("suiteTypeFilter");
        if (!toolbar || !typeFilter) return;

        const counts = {};
        shownCases.forEach((tc) => { const s = sourceOf(tc); counts[s] = (counts[s] || 0) + 1; });
        const previous = selected();
        const select = document.createElement("select");
        select.id = "suiteSourceFilter";
        select.style.width = "auto";
        select.title = "Run only the test cases from this source";
        select.innerHTML = `<option value="">All sources</option>` +
            Object.keys(LABELS).filter((k) => counts[k])
                .map((k) => `<option value="${k}">${esc(LABELS[k])} (${counts[k]})</option>`).join("");
        if (previous && counts[previous]) select.value = previous;
        select.addEventListener("change", () => window.applySuiteFilter());
        const old = document.getElementById("suiteSourceFilter");
        if (old) old.remove();
        typeFilter.insertAdjacentElement("afterend", select);

        shownCases.forEach((tc, i) => {
            const card = document.getElementById(`suiteCard-${i + 1}`);
            if (!card) return;
            const src = sourceOf(tc);
            card.dataset.source = src;
            const title = card.querySelector(".scenario-title");
            if (title && !title.querySelector(".source-badge")) {
                const badge = document.createElement("span");
                badge.className = `tag source-badge source-${src}`;
                badge.textContent = LABELS[src];
                title.appendChild(badge);
            }
        });
        if (select.value) window.applySuiteFilter();
    }

    const originalRender = window.renderSuiteList;
    if (typeof originalRender === "function") {
        window.renderSuiteList = function (cases) {
            const result = originalRender.apply(this, arguments);
            try { decorate(cases); } catch (e) { /* never break the suite list */ }
            return result;
        };
    }

    // Hide cards from other sources on top of the existing text/type filter.
    const originalFilter = window.applySuiteFilter;
    if (typeof originalFilter === "function") {
        window.applySuiteFilter = function () {
            const result = originalFilter.apply(this, arguments);
            const src = selected();
            if (!src) return result;
            let shown = 0;
            document.querySelectorAll(".test-case-card").forEach((card) => {
                if (card.dataset.source !== src) card.style.display = "none";
                else if (card.style.display !== "none") shown++;
            });
            const info = document.getElementById("suiteFilterInfo");
            if (info) {
                info.innerText = `Showing ${shown} of ${shownCases.length} scenarios — ` +
                    `Run suite will execute only ${LABELS[src]} cases.`;
                info.style.display = "";
            }
            return result;
        };
    }

    // app.js builds the run URL inline; add the chosen source to it.
    const NativeEventSource = window.EventSource;
    if (NativeEventSource) {
        const Patched = function (url, init) {
            const src = selected();
            if (typeof url === "string" && url.startsWith("/run-matrix-stream")) {
                if (src && !/[?&]sources=/.test(url)) {
                    url += `&sources=${encodeURIComponent(src)}`;
                }
                // Test speed (Run & verify -> Execution profile).
                const speed = (document.getElementById("speedSelect") || {}).value;
                if (speed && !/[?&]speed=/.test(url)) {
                    url += `&speed=${encodeURIComponent(speed)}`;
                }
            }
            return new NativeEventSource(url, init);
        };
        Patched.prototype = NativeEventSource.prototype;
        ["CONNECTING", "OPEN", "CLOSED"].forEach((k) => { Patched[k] = NativeEventSource[k]; });
        window.EventSource = Patched;
    }
})();
