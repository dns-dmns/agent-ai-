"""Browser-side probes shared by the recorder and the execution engine.

There used to be two implementations of "is this field mandatory?" — one in
``recorder.py`` and one in ``executor.py`` — and they disagreed. The recorder
required ``ng-invalid`` *and* ``ng-pristine``; the engine accepted a trailing
asterisk or ``aria-required``. So the same control on the same screen was
recorded as mandatory and then judged optional, and fixing one copy changed
nothing visible because the other still reported the opposite. That is the whole
reason "it is not capturing the mandatory fields" survived several attempts.

One definition, injected into both. A rule added here is a rule both sides get.

These are raw strings on purpose: the engine's JS lived in a non-raw ``\"\"\"``
literal where every regex needed doubled backslashes, and a single missed one
silently changed what a pattern matched.
"""

from __future__ import annotations

#: True when the element is actually on screen. A closed dialog left in the DOM
#: ahead of the real one is the common case this defends against.
ON_SCREEN_JS = r"""
const qaOnScreen = (el) => {
    if (!el) return false;
    try {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    } catch (e) { return false; }
};
"""

#: The label element for a control, and its text. Kept together because a marker
#: drawn by CSS lives on the element and appears in no text node at all.
LABEL_JS = r"""
const qaCssEscape = (v) => (window.CSS && CSS.escape) ? CSS.escape(v) : v;

const qaLabelElFor = (el) => {
    try {
        if (el.id) {
            const l = document.querySelector('label[for="' + qaCssEscape(el.id) + '"]');
            if (l) return l;
        }
        const wrap = el.closest('.field, .form-group, .p-field, .form-row, .mb-3, .col, .grid');
        const l2 = wrap && wrap.querySelector('label');
        if (l2) return l2;
    } catch (e) {}
    return null;
};

const qaLabelTextFor = (el) => {
    const l = qaLabelElFor(el);
    if (l && (l.innerText || '').trim()) return l.innerText.trim();
    return el.getAttribute('placeholder') || el.getAttribute('formcontrolname') || '';
};
"""

#: Why a control is mandatory, or '' when nothing says it is.
#:
#: Returning the *reason* rather than a boolean keeps the inference auditable: a
#: wrong guess is visible in ``learned.json`` as the signal that produced it,
#: instead of an unexplained ``true``.
#:
#: The signals, and why each is here:
#:   attr / aria / ng-reflect  declared outright — trust immediately
#:   asterisk                  anywhere in the label, not just the end. "*Code"
#:                             is as common as "Code *", and an end-anchored test
#:                             matched neither once the marker sat in its own span
#:   css-marker                ``label::after { content: "*" }`` reaches no text
#:                             node, so innerText can never see it
#:   label-class/wrapper-class some apps mark the row, not the text
#:   ng-invalid-empty          Angular marks a control invalid the moment it
#:                             renders empty with a required validator. Only
#:                             trusted while the field is STILL empty: afterwards
#:                             ng-invalid means "bad value", a different claim
REQUIRED_JS = r"""
const qaRequiredInfo = (el, labelEl, labelText) => {
    if (!el) return '';
    try {
        if (el.required) return 'attr';
        if (el.getAttribute('aria-required') === 'true') return 'aria';
        if (el.getAttribute('ng-reflect-required') === 'true') return 'ng-reflect';

        if (/\*/.test(labelText || '')) return 'asterisk';
        if (labelEl) {
            if (/\*/.test(labelEl.innerText || '')) return 'asterisk';
            for (const pseudo of ['::after', '::before']) {
                try {
                    const c = getComputedStyle(labelEl, pseudo).content || '';
                    if (c.indexOf('*') !== -1) return 'css-marker';
                } catch (e) {}
            }
            if (/\b(required|mandatory)\b/i.test(labelEl.className || '')) return 'label-class';
        }

        const wrap = el.closest('.field, .form-group, .p-field, .form-row');
        if (wrap && /\b(required|mandatory)\b/i.test(wrap.className || '')) return 'wrapper-class';

        const cls = String(el.className || '') + ' ' +
                    String((el.closest('.p-inputwrapper') || {}).className || '');
        const empty = !String(el.value == null ? '' : el.value).trim();
        if (empty && /\bng-invalid\b/.test(cls)) return 'ng-invalid-empty';
    } catch (e) {}
    return '';
};

const qaIsRequired = (el) => !!qaRequiredInfo(el, qaLabelElFor(el), qaLabelTextFor(el));
"""

#: Everything above, in dependency order. Inject this once per script.
SHARED_PROBES_JS = ON_SCREEN_JS + LABEL_JS + REQUIRED_JS
