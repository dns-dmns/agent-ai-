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
* **Codegen-style capture** — every event carries the element's full visible
  text (``text_full``), computed ARIA ``role``, ``accessible_name`` and the
  text-bearing attributes, so a recording can be reviewed for UI wording and
  spelling, not just replayed. An exact ``text="…"`` locator (Playwright's text
  engine, what codegen's get_by_text produces) is appended to the candidate
  list for clickable controls.
* **Assert mode** — codegen's "assert text" tool. While ``window.__qaAssertMode``
  is true a click does not act on the page; it records a ``VERIFY_TEXT`` /
  ``VERIFY_VALUE`` / ``VERIFY_VISIBLE`` step carrying the element's exact
  current text, so a later run fails if the wording ever changes. Toggled live
  through ``POST /set-record-mode``.
"""

from .dom_probes import SHARED_PROBES_JS

JS_RECORDER_SCRIPT = r"""
(function() {
    if (window.hasPlaywrightRecorder) return;
    window.hasPlaywrightRecorder = true;
__SHARED_PROBES__

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
            // Playwright's own text engine, exact match — the locator
            // `codegen` reaches for first (get_by_text / get_by_role name).
            // Last among the text candidates because it is the most brittle
            // to a wording change, which is exactly what an assertion should
            // catch and a click should not depend on.
            if (tag === 'button' || tag === 'a' || tag === 'label' ||
                el.getAttribute('role') === 'button' ||
                el.getAttribute('role') === 'tab' ||
                el.getAttribute('role') === 'menuitem' ||
                el.getAttribute('role') === 'option') {
                push('text="' + quote(text) + '"');
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

    // ── Codegen-style element description ─────────────────────────────
    // The recording used to keep text truncated to 50 characters, which is
    // enough to replay a click and useless for reviewing what the screen
    // actually says. Capturing the full text, the computed role and the
    // text-bearing attributes is what makes a recording auditable for wording
    // and spelling — the thing Playwright's inspector shows you live.
    //
    // Deliberately NOT captured: the value of any password/sensitive input.
    // app/credentials.py scrubs the `value` and `text` keys of a TYPE action;
    // a second copy of the same literal under a new key would walk straight
    // past it and back into actions.json, which is committed.
    var SENSITIVE_NAME = /pass|pwd|otp|captcha|secret|token|cvv|pin\b/i;

    function isSensitiveField(el) {
        try {
            var type = (el.getAttribute('type') || '').toLowerCase();
            if (type === 'password') return true;
            var hay = [el.id, el.name, el.getAttribute('formcontrolname'),
                       el.getAttribute('aria-label'), el.placeholder,
                       labelTextFor(el)].join(' ');
            return SENSITIVE_NAME.test(hay);
        } catch (e) { return true; }   // unreadable ⇒ treat as sensitive
    }

    // Implicit ARIA roles — the common subset, which is what a locator needs.
    function implicitRole(el) {
        var explicit = el.getAttribute && el.getAttribute('role');
        if (explicit) return explicit.trim();
        var tag = el.tagName.toLowerCase();
        var type = (el.getAttribute('type') || '').toLowerCase();
        if (tag === 'a') return el.hasAttribute('href') ? 'link' : '';
        if (tag === 'button') return 'button';
        if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'img') return 'img';
        if (tag === 'table') return 'table';
        if (tag === 'ul' || tag === 'ol') return 'list';
        if (tag === 'li') return 'listitem';
        if (/^h[1-6]$/.test(tag)) return 'heading';
        if (tag === 'input') {
            if (type === 'checkbox') return 'checkbox';
            if (type === 'radio') return 'radio';
            if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
            if (type === 'search') return 'searchbox';
            if (type === 'number') return 'spinbutton';
            if (type === 'range') return 'slider';
            if (['text', 'email', 'tel', 'url', ''].indexOf(type) !== -1) return 'textbox';
        }
        return '';
    }

    // The name a screen reader would announce — also what get_by_role(name=…)
    // matches against.
    function accessibleName(el) {
        try {
            var aria = el.getAttribute('aria-label');
            if (aria && aria.trim()) return aria.trim();
            var labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
                var parts = labelledBy.split(/\s+/).map(function (id) {
                    var ref = document.getElementById(id);
                    return ref ? (ref.innerText || '').trim() : '';
                }).filter(Boolean);
                if (parts.length) return parts.join(' ');
            }
            var lab = labelTextFor(el);
            if (lab) return lab;
            var title = el.getAttribute('title');
            if (title && title.trim()) return title.trim();
            if (el.tagName.toLowerCase() === 'img') {
                var alt = el.getAttribute('alt');
                if (alt && alt.trim()) return alt.trim();
            }
            return (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 120);
        } catch (e) { return ''; }
    }

    var TEXT_ATTRS = ['title', 'placeholder', 'aria-label', 'alt', 'href',
                      'name', 'type', 'role'];

    function describeElement(el) {
        var out = { role: '', accessible_name: '', text_full: '', attrs: {} };
        try {
            out.role = implicitRole(el);
            out.accessible_name = accessibleName(el);
            // Full visible text, whitespace-collapsed. Capped generously: a
            // paragraph is worth keeping for a spelling review, a whole table
            // body is not.
            var full = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
            if (full && full.length <= 600) out.text_full = full;
            else if (full) out.text_full = full.slice(0, 600) + '…';
            TEXT_ATTRS.forEach(function (attr) {
                var v = el.getAttribute && el.getAttribute(attr);
                if (v && String(v).trim()) out.attrs[attr] = String(v).trim().slice(0, 200);
            });
            if (el.tagName.toLowerCase() === 'input' ||
                el.tagName.toLowerCase() === 'textarea') {
                out.attrs.disabled = el.disabled ? 'true' : 'false';
                out.attrs.readonly = el.readOnly ? 'true' : 'false';
            }
        } catch (e) {}
        return out;
    }

    // Mandatory-field detection lives in app/dom_probes.py and is injected
    // above, so the recorder and the execution engine cannot drift apart. They
    // did: this file demanded ng-invalid AND ng-pristine while the engine
    // accepted a trailing asterisk, so one screen was recorded mandatory and
    // judged optional.
    function isRequired(el) { return qaIsRequired(el); }

    function emit(action, el, extra) {
        var selectors = candidateSelectors(el);
        var tag = el.tagName ? el.tagName.toLowerCase() : '';
        var described = describeElement(el);
        var payload = {
            action: action,
            selector: selectors[0] || structuralPath(el),
            selectors: selectors,
            tag: tag,
            text: (el.innerText ? el.innerText.trim().substring(0, 50) : ''),
            label: labelTextFor(el),
            required: isRequired(el),
            value: '',
            // Codegen-style capture. Older recordings simply lack these keys and
            // every consumer already uses .get(), so nothing downstream changes.
            role: described.role,
            accessible_name: described.accessible_name,
            text_full: described.text_full,
            attrs: described.attrs
        };
        if (extra) {
            for (var k in extra) { if (extra.hasOwnProperty(k)) payload[k] = extra[k]; }
        }
        // A sensitive field must not carry an echo of what was typed into it in
        // ANY key. credentials.scrub_recorded_actions cleans `value` and `text`;
        // these two would otherwise smuggle the same literal past it.
        if (isSensitiveField(el)) {
            payload.text_full = '';
            payload.accessible_name = labelTextFor(el) || '';
            if (payload.attrs) { delete payload.attrs.value; }
        }
        send(payload);
        // Watch what this action caused. CLICK and SELECT are what change state;
        // watching every keystroke would be noise.
        if (action === 'CLICK' || action === 'SELECT' || action === 'CHECK'
                || action === 'UNCHECK') {
            captureEffect(action, payload.selector);
        }
    }

    // ── Effect capture ────────────────────────────────────────────────
    // A recording used to say only *what was clicked*. It said nothing about what
    // happened next, so the generation agents had to invent the expected result
    // and the runner had to guess where this application renders its messages.
    //
    // After each action, watch for ~1.2s and report the effect: the URL change,
    // any text that appeared, and elements that came or went. The exact wording
    // is captured while recording, so a scenario can assert the message the
    // application really shows instead of one the model made up.
    var effectSeq = 0;

    function visibleNow(el) {
        try {
            var rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return false;
            var style = getComputedStyle(el);
            return style.visibility !== 'hidden' && style.display !== 'none';
        } catch (e) { return false; }
    }

    function idsOnScreen() {
        var out = {};
        try {
            document.querySelectorAll('[id]').forEach(function (el) {
                if (el.id && visibleNow(el)) out[el.id] = true;
            });
        } catch (e) {}
        return out;
    }

    function describeNode(el) {
        if (!el || !el.tagName) return '';
        var tag = el.tagName.toLowerCase();
        var cls = (typeof el.className === 'string' ? el.className : '')
            .trim().split(/\s+/).filter(Boolean).slice(0, 3);
        return cls.length ? tag + '.' + cls.join('.') : tag;
    }

    var NOISE_TEXT = [
        /^[\d\s.,:%/-]+$/,
        /^\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?$/i,
        /^(showing|page|rows?|total|of|items?)/i
    ];

    function captureEffect(action, selector) {
        var token = ++effectSeq;
        var urlBefore = location.href;
        var idsBefore = idsOnScreen();
        var messages = [];

        var observer = new MutationObserver(function (records) {
            records.forEach(function (record) {
                var nodes = [];
                record.addedNodes.forEach(function (n) { nodes.push(n); });
                if (record.type === 'characterData') nodes.push(record.target);
                nodes.forEach(function (node) {
                    if (node && node.nodeType === Node.TEXT_NODE) node = node.parentElement;
                    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
                    var text = (node.innerText || node.textContent || '').trim();
                    if (!text || text.length > 200) return;
                    if (text.split(/\s+/).length > 25) return;
                    if (NOISE_TEXT.some(function (re) { return re.test(text); })) return;
                    if (node.closest && node.closest('tbody, table, [role="rowgroup"]')) return;
                    if (!visibleNow(node)) return;
                    if (messages.some(function (m) { return m.text === text; })) return;
                    messages.push({ text: text, selector: describeNode(node) });
                });
            });
        });

        try {
            observer.observe(document.body, {
                childList: true, subtree: true, characterData: true
            });
        } catch (e) { return; }

        setTimeout(function () {
            observer.disconnect();
            if (token !== effectSeq) return;       // a newer action superseded us
            var idsAfter = idsOnScreen();
            var appeared = Object.keys(idsAfter).filter(function (id) {
                return !idsBefore[id];
            }).slice(0, 12);
            var disappeared = Object.keys(idsBefore).filter(function (id) {
                return !idsAfter[id];
            }).slice(0, 12);
            var urlAfter = location.href;
            if (!messages.length && urlBefore === urlAfter
                && !appeared.length && !disappeared.length) {
                return;                            // nothing observable happened
            }
            send({
                action: '__EFFECT__',
                selector: selector || '',
                for_action: action,
                effect: {
                    url_before: urlBefore,
                    url_after: urlAfter,
                    messages: messages.slice(0, 6),
                    appeared: appeared,
                    disappeared: disappeared
                }
            });
        }, 1200);
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
        if (window.__qaAssertMode) return;   // annotating, not filling the form
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

    // ── Assert mode ───────────────────────────────────────────────────
    // Playwright codegen's "assert text" tool. While the toggle is on, a click
    // does not drive the application — it captures what the element says right
    // now as a VERIFY_* step. Re-running the suite then fails if the wording,
    // the value or the visibility ever changes, which is what makes a spelling
    // mistake a test failure instead of something a human has to notice.
    //
    // The flag lives on window so the server can flip it mid-session via
    // page.evaluate without re-injecting the whole recorder.
    if (typeof window.__qaAssertMode === 'undefined') window.__qaAssertMode = false;

    function assertOutline(el) {
        try {
            var previous = el.style.outline;
            el.style.outline = '2px solid #22c55e';
            setTimeout(function () { el.style.outline = previous; }, 450);
        } catch (e) {}
    }

    function recordAssertion(el) {
        var selectors = candidateSelectors(el);
        var tag = el.tagName ? el.tagName.toLowerCase() : '';
        var described = describeElement(el);
        var action, value;

        if (isTextField(el) || tag === 'select') {
            // Never assert on a password: the expected value would be the
            // credential, written into test_cases.json in clear.
            if (isSensitiveField(el)) {
                action = 'VERIFY_VISIBLE';
                value = '';
            } else {
                action = 'VERIFY_VALUE';
                value = readValue(el);
                if (tag === 'select') {
                    try {
                        value = el.options[el.selectedIndex].text.trim();
                    } catch (err) {}
                }
            }
        } else {
            var text = described.text_full ||
                       (el.innerText || '').trim().replace(/\s+/g, ' ');
            if (text) {
                action = 'VERIFY_TEXT';
                value = text;
            } else {
                action = 'VERIFY_VISIBLE';
                value = '';
            }
        }

        assertOutline(el);
        send({
            action: action,
            selector: selectors[0] || structuralPath(el),
            selectors: selectors,
            tag: tag,
            text: (el.innerText ? el.innerText.trim().substring(0, 50) : ''),
            label: labelTextFor(el),
            required: false,
            value: value,
            role: described.role,
            accessible_name: described.accessible_name,
            text_full: isSensitiveField(el) ? '' : described.text_full,
            attrs: described.attrs,
            asserted: true
        });
    }

    // Capture phase, before anything else, so the application never sees the
    // click while the tester is annotating the page.
    document.addEventListener('click', function(e) {
        if (!window.__qaAssertMode) return;
        e.preventDefault();
        e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        var el = e.target;
        if (el && el.nodeType === Node.ELEMENT_NODE) recordAssertion(el);
    }, true);

    // ── Clicks ────────────────────────────────────────────────────────
    document.addEventListener('click', function(e) {
        if (window.__qaAssertMode) return;      // the assert handler took it
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
        if (window.__qaAssertMode) return;
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
        if (window.__qaAssertMode) return;
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


# Injected rather than concatenated so the placeholder sits inside the IIFE,
# where the helpers it defines are in scope for everything below it.
JS_RECORDER_SCRIPT = JS_RECORDER_SCRIPT.replace("__SHARED_PROBES__", SHARED_PROBES_JS)
