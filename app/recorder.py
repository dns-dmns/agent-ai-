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

        // A control inside a modal must say so. Otherwise a selector like
        // 'input[placeholder="Days"]' or 'p-dropdown > div' also matches a
        // look-alike on the page behind the dialog, and the replay types into
        // the wrong one — the value never reaches the form the tester filled.
        var surface = el.closest('p-dialog, .p-dialog, [role="dialog"], dialog, .modal, ' +
                                 '.mat-dialog-container, .mat-mdc-dialog-container, ' +
                                 '.ant-modal, .MuiDialog-container, .cdk-overlay-pane');
        if (surface) {
            var scope = surface.tagName.toLowerCase();
            var scopedBase = [];
            if (el.id && isStableId(el.id)) scopedBase.push('#' + cssEscape(el.id));
            if (fcn) scopedBase.push(tag + '[formcontrolname="' + quote(fcn) + '"]');
            if (el.name) scopedBase.push(tag + '[name="' + quote(el.name) + '"]');
            if (el.placeholder) scopedBase.push(tag + '[placeholder="' + quote(el.placeholder) + '"]');
            scopedBase.forEach(function(base) { push(scope + ' ' + base); });
        }

        var text = (el.innerText || '').trim().split('\n')[0];
        if (text && text.length < 40) {
            if (tag === 'button' || el.getAttribute('role') === 'button') {
                push('button:has-text("' + quote(text) + '")');
            } else if (tag === 'a') {
                push('a:has-text("' + quote(text) + '")');
            }
        }

        // Icon-only controls. A grid's action column is usually four unlabelled
        // icon buttons (view / edit / history / delete); recorded as
        // 'td:nth-of-type(12) > button' they are indistinguishable, so neither a
        // replay nor the generator can tell which one does what. The tooltip and
        // the icon class name carry that meaning.
        if (!text && (tag === 'button' || tag === 'a' || el.getAttribute('role') === 'button')) {
            var title = el.getAttribute('title') || el.getAttribute('aria-label') ||
                        el.getAttribute('pTooltip') || el.getAttribute('ng-reflect-text') || '';
            if (title) {
                push(tag + '[title="' + quote(title) + '"]');
                push('[aria-label="' + quote(title) + '"]');
            }
            var icon = el.querySelector('i[class], span[class*="pi-"], mat-icon, svg[class]');
            if (icon) {
                var iconClasses = String(icon.getAttribute('class') || '').split(/\s+/);
                for (var ic = 0; ic < iconClasses.length; ic++) {
                    var cls = iconClasses[ic];
                    // pi-history, pi-eye, pi-pencil, pi-trash, fa-download, …
                    if (/^(pi|fa|bi|mdi|material-icons|icon)-[a-z0-9-]+$/i.test(cls) &&
                        cls.indexOf('-fw') === -1) {
                        push(tag + ':has(' + icon.tagName.toLowerCase() + '.' + cls + ')');
                    }
                }
                var iconText = (icon.innerText || '').trim();
                if (iconText && iconText.length < 24) {
                    push(tag + ':has-text("' + quote(iconText) + '")');
                }
            }
            // Scoped to its row, so it survives re-ordering and paging.
            var row = el.closest('tr');
            if (row) {
                var rowText = (row.innerText || '').trim().split('\n')[0];
                if (rowText && rowText.length < 40 && (title || icon)) {
                    var inner = title ? ('[title="' + quote(title) + '"]') : tag;
                    push('tr:has-text("' + quote(rowText) + '") ' + inner);
                }
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

    // A field the application marks as mandatory. Knowing this is what stops the
    // agents generating a "click Save" scenario that can never work, because the
    // application keeps Save disabled until every required field is filled.
    function isRequired(el) {
        try {
            if (el.required || el.getAttribute('aria-required') === 'true') return true;
            var wrapper = el.closest('.field, .form-group, .p-field, .form-row, .mb-3') ||
                          el.parentElement;
            if (wrapper) {
                var lab = wrapper.querySelector('label');
                // A trailing asterisk is the near-universal convention for it.
                if (lab && /\*\s*$/.test(lab.innerText || '')) return true;
                if (wrapper.querySelector('.required, .p-error, [class*="required"]')) return true;
            }
            var host = el.closest('[formcontrolname]') || el;
            if (host && /\bng-invalid\b/.test(host.className || '') &&
                /\bng-pristine\b/.test(host.className || '')) return true;
        } catch (e) {}
        return false;
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
            required: isRequired(el),
            value: ''
        };
        if (extra) {
            for (var k in extra) { if (extra.hasOwnProperty(k)) payload[k] = extra[k]; }
        }
        send(payload);
    }

    // ── Value tracking ────────────────────────────────────────────────
    // Component libraries (PrimeNG InputNumber/Calendar, Material, Ant) write the
    // value programmatically, which fires NO native 'change' event — so a field
    // the tester filled was recorded as if it had never been touched. Tracking
    // the value across focus catches those regardless of how it was set.
    var valueAtFocus = new WeakMap();
    var valueRecorded = new WeakMap();

    function readValue(el) {
        try { return el.value == null ? '' : String(el.value); } catch (err) { return ''; }
    }

    function isTextField(el) {
        if (!el || !el.tagName) return false;
        var tag = el.tagName.toLowerCase();
        if (tag !== 'input' && tag !== 'textarea') return false;
        var type = (el.getAttribute('type') || '').toLowerCase();
        return ['checkbox', 'radio', 'file', 'submit', 'button', 'reset', 'image']
            .indexOf(type) === -1;
    }

    function flushValue(el) {
        if (!isTextField(el)) return;
        var now = readValue(el);
        var before = valueAtFocus.has(el) ? valueAtFocus.get(el) : '';
        if (now === before) return;                       // nothing changed
        if (valueRecorded.get(el) === now) return;        // 'change' already got it
        valueRecorded.set(el, now);
        valueAtFocus.set(el, now);
        emit('TYPE', el, { value: now });
    }

    document.addEventListener('focusin', function(e) {
        if (isTextField(e.target)) valueAtFocus.set(e.target, readValue(e.target));
    }, true);

    document.addEventListener('focusout', function(e) {
        flushValue(e.target);
    }, true);

    // ── Clicks ────────────────────────────────────────────────────────
    document.addEventListener('click', function(e) {
        // A click on Save must not be recorded before the value typed into the
        // field that still has focus.
        var focused = document.activeElement;
        if (focused && focused !== e.target) flushValue(focused);

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
            valueRecorded.set(target, target.value);
            valueAtFocus.set(target, target.value);
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
