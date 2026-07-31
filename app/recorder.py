"""Injected browser recorder.

Two upgrades over the original script:

* **Selector resilience** — every action now carries an ordered ``selectors``
  array (data-testid → stable id → formcontrolname → name → aria-label →
  placeholder → label text → button text → structural path). Framework-generated
  ids such as PrimeNG's ``pn_id_8`` or Angular Material's ``mat-input-3`` are
  recognised as unstable and demoted, which is what used to break replays after
  a redeploy. ``selector`` still holds the single best guess, so old recordings
  and old code paths keep working unchanged.
* **Coverage** — besides clicks and typing it now records dropdown selections,
  checkbox state, Enter/Tab/Escape key presses, file uploads and SPA route
  changes, which previously vanished from the recording entirely.
"""

JS_RECORDER_SCRIPT = r"""
(function() {
    if (window.hasPlaywrightRecorder) return;
    window.hasPlaywrightRecorder = true;

    var UNSTABLE_ID = /^(pn_id_|p-|mat-|cdk-|ng-|ui-id|:r[0-9a-z]|react-|radix-|headlessui-|el-id-|__)/i;
    var RANDOMISH = /^[a-f0-9]{8,}$/i;

    function isStableId(id) {
        if (!id) return false;
        if (UNSTABLE_ID.test(id)) return false;
        if (RANDOMISH.test(id)) return false;
        if (/\d{4,}$/.test(id)) return false;
        return true;
    }

    function cssEscape(value) {
        if (window.CSS && CSS.escape) { try { return CSS.escape(value); } catch (e) {} }
        return String(value).replace(/["\\\]\[\(\)#\.:>\+~\*\^\$\|=,\s]/g, '\\$&');
    }

    function quote(value) {
        return String(value).replace(/"/g, '\\"');
    }

    function labelTextFor(el) {
        try {
            if (el.id) {
                var lab = document.querySelector('label[for="' + cssEscape(el.id) + '"]');
                if (lab && lab.innerText.trim()) return lab.innerText.trim();
            }
            var parentLabel = el.closest('label');
            if (parentLabel && parentLabel.innerText.trim()) return parentLabel.innerText.trim();
            var wrapper = el.closest('.field, .form-group, .p-field, .form-row, .mb-3');
            if (wrapper) {
                var lab2 = wrapper.querySelector('label');
                if (lab2 && lab2.innerText.trim()) return lab2.innerText.trim();
            }
        } catch (e) {}
        return '';
    }

    function structuralPath(el) {
        var path = [];
        var current = el;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
            var selector = current.tagName.toLowerCase();
            if (current.id && isStableId(current.id)) {
                path.unshift(selector + '#' + cssEscape(current.id));
                break;
            }
            var sib = current, nth = 1;
            while ((sib = sib.previousElementSibling)) {
                if (sib.tagName === current.tagName) nth++;
            }
            if (nth > 1) selector += ':nth-of-type(' + nth + ')';
            path.unshift(selector);
            current = current.parentElement;
            if (path.length > 8) break;
        }
        return path.join(' > ');
    }

    function candidateSelectors(el) {
        var tag = el.tagName.toLowerCase();
        var out = [];
        function push(sel) {
            if (sel && out.indexOf(sel) === -1) out.push(sel);
        }

        ['data-testid', 'data-test', 'data-cy', 'data-qa'].forEach(function(attr) {
            var v = el.getAttribute && el.getAttribute(attr);
            if (v) push('[' + attr + '="' + quote(v) + '"]');
        });
        if (el.id && isStableId(el.id)) push('#' + cssEscape(el.id));
        var fcn = el.getAttribute && el.getAttribute('formcontrolname');
        if (fcn) push(tag + '[formcontrolname="' + quote(fcn) + '"]');
        if (el.name) push(tag + '[name="' + quote(el.name) + '"]');
        var aria = el.getAttribute && el.getAttribute('aria-label');
        if (aria) push('[aria-label="' + quote(aria) + '"]');
        if (el.placeholder) push(tag + '[placeholder="' + quote(el.placeholder) + '"]');

        var label = labelTextFor(el);
        if (label && (tag === 'input' || tag === 'textarea' || tag === 'select')) {
            push(tag + '[placeholder="' + quote(label) + '"]');
        }

        var text = (el.innerText || '').trim().split('\n')[0];
        if (text && text.length < 40) {
            if (tag === 'button' || el.getAttribute('role') === 'button') {
                push('button:has-text("' + quote(text) + '")');
            } else if (tag === 'a') {
                push('a:has-text("' + quote(text) + '")');
            }
        }
        if (el.id && !isStableId(el.id)) push('#' + cssEscape(el.id));
        push(structuralPath(el));
        return out;
    }

    function send(payload) {
        try {
            if (window.recordPlaywrightEvent) window.recordPlaywrightEvent(payload);
        } catch (e) {}
    }

    function emit(action, el, extra) {
        var selectors = candidateSelectors(el);
        var tag = el.tagName ? el.tagName.toLowerCase() : '';
        var payload = {
            action: action,
            selector: selectors[0] || structuralPath(el),
            selectors: selectors,
            tag: tag,
            text: (el.innerText ? el.innerText.trim().substring(0, 50) : ''),
            label: labelTextFor(el),
            value: ''
        };
        if (extra) {
            for (var k in extra) { if (extra.hasOwnProperty(k)) payload[k] = extra[k]; }
        }
        send(payload);
    }

    // ── Clicks ────────────────────────────────────────────────────────
    document.addEventListener('click', function(e) {
        var target = e.target.closest(
            'button, input, select, textarea, a, [role="button"], [role="option"], ' +
            '.p-dropdown, .p-dropdown-item, .p-checkbox, .mat-mdc-select, .ant-select, ' +
            'li[role="menuitem"], .p-button, [role="tab"]'
        );
        if (!target) return;
        var type = (target.getAttribute && target.getAttribute('type')) || '';
        if (target.tagName.toLowerCase() === 'input' &&
            (type === 'checkbox' || type === 'radio')) {
            emit(target.checked ? 'CHECK' : 'UNCHECK', target);
            return;
        }
        emit('CLICK', target);
    }, true);

    // ── Typing / selecting ────────────────────────────────────────────
    document.addEventListener('change', function(e) {
        var target = e.target;
        if (!target || !target.tagName) return;
        var tag = target.tagName.toLowerCase();
        if (tag === 'select') {
            var optionText = '';
            try {
                optionText = target.options[target.selectedIndex].text.trim();
            } catch (err) {}
            emit('SELECT', target, { value: optionText || target.value });
            return;
        }
        if (tag === 'input' || tag === 'textarea') {
            var type = (target.getAttribute('type') || '').toLowerCase();
            if (type === 'checkbox' || type === 'radio') {
                emit(target.checked ? 'CHECK' : 'UNCHECK', target);
                return;
            }
            if (type === 'file') {
                var names = [];
                try {
                    for (var i = 0; i < target.files.length; i++) names.push(target.files[i].name);
                } catch (err) {}
                emit('UPLOAD', target, { value: names.join(',') });
                return;
            }
            emit('TYPE', target, { value: target.value });
        }
    }, true);

    // ── Key presses that carry meaning ────────────────────────────────
    document.addEventListener('keydown', function(e) {
        if (['Enter', 'Tab', 'Escape'].indexOf(e.key) === -1) return;
        var target = e.target;
        if (!target || !target.tagName) return;
        var tag = target.tagName.toLowerCase();
        if (e.key === 'Tab' && tag !== 'input') return;
        emit('KEYPRESS', target, { value: e.key });
    }, true);

    // ── SPA route changes ─────────────────────────────────────────────
    (function trackNavigation() {
        var lastUrl = location.href;
        function report() {
            if (location.href === lastUrl) return;
            lastUrl = location.href;
            send({
                action: 'NAVIGATE', selector: location.href, selectors: [location.href],
                tag: 'window', text: document.title || '', value: location.href
            });
        }
        ['pushState', 'replaceState'].forEach(function(method) {
            var original = history[method];
            if (!original) return;
            history[method] = function() {
                var result = original.apply(this, arguments);
                setTimeout(report, 60);
                return result;
            };
        });
        window.addEventListener('popstate', function() { setTimeout(report, 60); });
        window.addEventListener('hashchange', function() { setTimeout(report, 60); });
    })();
})();
"""
