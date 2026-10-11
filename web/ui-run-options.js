/* Run options: Screens, Test speed (with custom values), and which types run.

   - Screens / Test speed "Custom…" show a number box. A valid number becomes a
     real option ("5 screens (custom)", "custom:6") and is selected, so app.js
     -- which reads #workersSelect.value as a number -- never sees "custom".
   - The run request gets &speed= (app/pacing.py).
   - The run uses the type chosen on the Run screen ("All types" filter), not
     the Generate tab's "Test-type focus" chips. Those chips are for generating;
     sending them made Run suite silently skip every ui / functional / security
     case (11 of 18 ran on Liberty `item`).
   - Choices are remembered per browser.
*/
(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const remember = (key, value) => { try { localStorage.setItem(key, value); } catch (e) {} };
    const recall = (key) => { try { return localStorage.getItem(key); } catch (e) { return null; } };

    function customOption(select, value, text) {
        let opt = [...select.options].find((o) => o.value === value);
        if (!opt) {
            opt = document.createElement("option");
            opt.value = value;
            opt.textContent = text;
            select.insertBefore(opt, select.querySelector('option[value="custom"]'));
        }
        select.value = value;
    }

    function setup(selectId, inputId, storageKey, parse) {
        const select = $(selectId), input = $(inputId);
        if (!select || !input) return;
        let previous = select.value;
        const apply = () => {
            const made = parse(input.value);
            if (!made) return;
            customOption(select, made.value, made.text);
            input.style.display = "none";
            previous = select.value;
            remember(storageKey, select.value);
        };
        select.addEventListener("change", () => {
            if (select.value === "custom") {
                input.style.display = "";
                input.focus();
            } else {
                input.style.display = "none";
                previous = select.value;
                remember(storageKey, select.value);
            }
        });
        input.addEventListener("change", apply);
        // Left without a valid number: go back to the last real choice, so
        // nothing ever reads "custom" as a value.
        input.addEventListener("blur", () => {
            if (select.value === "custom") {
                select.value = previous;
                input.style.display = "none";
            }
        });
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } });

        const saved = recall(storageKey);
        if (saved && saved !== "custom") {
            if (![...select.options].some((o) => o.value === saved)) {
                const made = parse(saved.replace(/^custom:/, ""));
                if (made && made.value === saved) customOption(select, made.value, made.text);
            } else {
                select.value = saved;
            }
            previous = select.value;
        }
    }

    setup("workersSelect", "workersCustom", "qa.screens", (raw) => {
        const n = parseInt(raw, 10);
        if (!Number.isFinite(n) || n < 1 || n > 16) return null;
        return { value: String(n), text: `${n} screen${n === 1 ? "" : "s"} (custom)` };
    });
    setup("speedSelect", "speedCustom", "qa.speed", (raw) => {
        const s = parseFloat(raw);
        if (!Number.isFinite(s) || s < 0.5 || s > 120) return null;
        return { value: `custom:${s}`, text: `Custom — wait at most ${s}s per step` };
    });

    // Rewrite the run request (app.js builds it inline).
    const NativeEventSource = window.EventSource;
    if (!NativeEventSource) return;
    const setParam = (url, key, value) => {
        const re = new RegExp(`([?&])${key}=[^&]*`);
        const part = `${key}=${encodeURIComponent(value)}`;
        return re.test(url) ? url.replace(re, `$1${part}`) : `${url}&${part}`;
    };
    const Patched = function (url, init) {
        if (typeof url === "string" && url.startsWith("/run-matrix-stream")) {
            const speed = ($("speedSelect") || {}).value || "normal";
            url = setParam(url, "speed", speed === "custom" ? "normal" : speed);
            if (/[?&]workers=custom\b/.test(url)) url = setParam(url, "workers", "1");
            url = setParam(url, "test_types", ($("suiteTypeFilter") || {}).value || "");
        }
        return new NativeEventSource(url, init);
    };
    Patched.prototype = NativeEventSource.prototype;
    ["CONNECTING", "OPEN", "CLOSED"].forEach((k) => { Patched[k] = NativeEventSource[k]; });
    window.EventSource = Patched;
})();
