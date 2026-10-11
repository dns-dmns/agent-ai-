"""Application explorer: map every screen of an application into recordings.

Give it a login URL; it logs in, finds every screen in the navigation menu, and
for each one writes ``flow.json`` + ``actions.json`` -- the same files a tester
produces with the recorder, and in exactly the same format, because the real
recorder writes them: the explorer injects ``recorder.JS_RECORDER_SCRIPT`` and
drives the page, and the recorder captures what happens (selectors, the effect
of each click, the messages that appeared).

For each screen it records:

- **checks** (recorder assert mode): page title, breadcrumb, table columns,
  search/filter boxes, buttons, every form label (with its ``*``). A check is a
  ``click`` event the recorder swallows in the capture phase -- the application
  never receives it, so a Delete or Approve button is documented, not pressed.
- **the Add form**, when there is one: an empty Save to capture the real
  "is required" messages (only on groups listed in ``EMPTY_SAVE_GROUPS``, and
  only when a mandatory field is empty), then every field filled with sample
  data and every dropdown opened and chosen, then **Cancel**.

Never done: saving a filled form, clicking a destructive button for real,
typing into password / OTP / captcha fields, uploading files. An existing
recording a person made is never overwritten. Read-only apart from the guarded
empty Save, which validation blocks; if it ever does not, the screen is flagged
``unexpected_save`` in the summary.

Usage::

    python -m app.explorer --project Liberty \\
        --url https://host/liberty/auth/login [--only Master] [--workers 3] [--headed]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from playwright.async_api import Page, async_playwright

from . import config, credentials as credential_store, pacing, recorder, screens, storage

log = config.get_logger("explorer")

#: Menu groups whose Add forms may receive one *empty* Save to capture the
#: required-field messages. Transaction screens (orders, dispatch, stock) are
#: never submitted, even empty.
EMPTY_SAVE_GROUPS = ("master",)

DANGER = re.compile(r"\b(delete|remove|approve|reject|submit|confirm|dispatch|post|"
                    r"logout|log out|sign out|cancel order|close order|issue|receive)\b", re.I)
LOGOUT = re.compile(r"\b(logout|log out|sign out|signout)\b", re.I)
ADD_BUTTON = re.compile(r"^\s*(\+\s*)?(add|new|create)\b", re.I)
CANCEL_BUTTON = re.compile(r"^\s*(cancel|close)\s*$", re.I)
SAVE_BUTTON = re.compile(r"^\s*(save|create|add|submit|update)\b", re.I)
SUCCESS_TEXT = re.compile(r"\b(success|successfully|added|created|saved)\b", re.I)

MAX_FIELDS = 60          # per form; a full-page order form can have hundreds
MAX_OPTIONS = 40         # dropdown options documented per field
EFFECT_WAIT_MS = 1400    # the recorder reports a click's effect after ~1.2 s

Emit = Callable[[str], Awaitable[None] | None]


# ──────────────────────────────────────────────────────────────────────
# Browser-side probes
# ──────────────────────────────────────────────────────────────────────
NAV_CONTAINER = ("app-sidebar, aside nav, nav[role=navigation], [role=navigation], "
                 "aside, .sidebar, .layout-sidebar, .p-panelmenu, .layout-menu")

# Menu items with their full path. A leaf is an item with no nested submenu;
# the path is built from the labels of the groups that contain it.
MENU_JS = """(containerSel) => {
  const box = document.querySelector(containerSel);
  if (!box) return [];
  const label = el => {
    const own = el.querySelector(':scope > span, :scope > a > span, :scope > a, :scope > .label, :scope > .menu-label');
    return ((own || el).innerText || '').trim().split('\\n')[0].replace(/\\s+/g, ' ');
  };
  const items = [...box.querySelectorAll('li, a[href], [role=menuitem]')]
    .filter(el => label(el) && label(el).length < 60);
  const out = [];
  items.forEach((el, i) => {
    // A group has a nested menu or an expand arrow, or is followed by the
    // container holding its children. A following *item* (another LI) whose
    // class merely contains "submenu" does not make this one a group.
    const next = el.nextElementSibling;
    const hasChildren = el.querySelector('ul, [role=menu]') ||
      el.querySelector(':scope > i.arrow, :scope > .p-submenu-icon, :scope > .arrow') ||
      (next && next.tagName !== 'LI' && /submenu|children|sub-menu|nested/.test(next.className || ''));
    if (hasChildren) return;
    const href = (el.getAttribute('href') || (el.querySelector('a') || {}).getAttribute?.('href') || '');
    if (/^(tel|mailto|javascript):/i.test(href)) return;
    if (el.tagName === 'A' && el.closest('li') && items.includes(el.closest('li'))) return;
    const path = [label(el)];
    let node = el.parentElement;
    while (node && node !== box) {
      let head = node.previousElementSibling;
      if (head && /submenu|children|sub-menu|nested/.test(node.className || '') && label(head)) {
        path.unshift(label(head));
      } else if (node.tagName === 'UL' && node.parentElement && node.parentElement.tagName === 'LI'
                 && node.parentElement !== el) {
        path.unshift(label(node.parentElement));
      }
      node = node.parentElement;
    }
    el.setAttribute('data-qa-menu', String(i));
    out.push({id: String(i), path: [...new Set(path)]});
  });
  return out;
}"""

FORM_ROOT_JS = """() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const dialogs = [...document.querySelectorAll('.p-dialog, [role=dialog], .modal.show, mat-dialog-container')].filter(vis);
  return dialogs.length ? 'dialog' : '';
}"""

# Visible form controls inside the open dialog (or the page), tagged so Python
# can address each one. Composite widgets are reported once, not per inner input.
FIELDS_JS = """(scopeSel) => {
  document.querySelectorAll('[data-qa-field]').forEach(e => e.removeAttribute('data-qa-field'));
  const scope = (scopeSel ? [...document.querySelectorAll(scopeSel)].filter(e => e.getBoundingClientRect().width > 0).pop() : null) || document.querySelector('app-pages, main') || document.body;
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const COMPOSITE = 'p-select, p-dropdown, p-multiselect, p-calendar, p-datepicker, p-inputnumber, p-checkbox, p-toggleswitch, p-inputswitch, p-autocomplete, mat-select, .ant-select';
  const out = [];
  scope.querySelectorAll('input, textarea, select, ' + COMPOSITE).forEach((el, i) => {
    if (!vis(el)) return;
    const comp = el.closest(COMPOSITE);
    if (comp && comp !== el) return;
    if (el.closest('table')) return;
    const tag = el.tagName.toLowerCase();
    const inner = el.matches(COMPOSITE) ? (el.querySelector('input') || el) : el;
    let label = '';
    const id = inner.id || el.id;
    if (id) { const l = document.querySelector(`label[for="${CSS.escape(id)}"]`); if (l) label = l.innerText; }
    if (!label) { let w = el.parentElement; for (let d = 0; d < 3 && w && !label; d++, w = w.parentElement) {
      const l = w.querySelector(':scope > label, :scope > .field-label, :scope > span.label'); if (l) label = l.innerText; } }
    label = (label || el.getAttribute('aria-label') || inner.getAttribute('placeholder') || '').trim().replace(/\\s+/g, ' ');
    let kind = 'text';
    const type = (inner.getAttribute('type') || '').toLowerCase();
    if (/p-select|p-dropdown|mat-select|ant-select/.test(tag + ' ' + el.className) || tag === 'select') kind = 'dropdown';
    else if (tag === 'p-multiselect') kind = 'multiselect';
    else if (/calendar|datepicker/.test(tag) || type === 'date') kind = 'date';
    else if (tag === 'p-inputnumber' || type === 'number' || inner.getAttribute('role') === 'spinbutton') kind = 'number';
    else if (/checkbox|toggleswitch|inputswitch/.test(tag) || type === 'checkbox' || type === 'radio') kind = 'toggle';
    else if (type === 'file') kind = 'file';
    else if (type === 'password') kind = 'password';
    else if (type === 'email') kind = 'email';
    else if (type === 'tel') kind = 'phone';
    else if (tag === 'p-autocomplete') kind = 'autocomplete';
    else if (tag === 'textarea') kind = 'textarea';
    const n = String(out.length);
    el.setAttribute('data-qa-field', n);
    out.push({
      n, kind, label: label.slice(0, 80),
      name: el.getAttribute('formcontrolname') || inner.getAttribute('name') || '',
      required: /\\*\\s*$/.test(label) || inner.required || inner.getAttribute('aria-required') === 'true',
      disabled: !!(inner.disabled || el.hasAttribute('disabled') || el.classList.contains('p-disabled')),
      readonly: !!(inner.readOnly || inner.hasAttribute('readonly')),
      empty: !(inner.value || '').trim() || /^0(\\.0+)?$/.test((inner.value || '').trim()),
      placeholder: inner.getAttribute('placeholder') || '',
    });
  });
  return out;
}"""

# Things worth a check step: headings, breadcrumb, column headers, labels.
CHECK_TARGETS_JS = """(scopeSel) => {
  document.querySelectorAll('[data-qa-check]').forEach(e => e.removeAttribute('data-qa-check'));
  const scope = (scopeSel ? [...document.querySelectorAll(scopeSel)].filter(e => e.getBoundingClientRect().width > 0).pop() : null) || document.querySelector('app-pages, main') || document.body;
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const seen = new Set(); const out = [];
  const add = (el, kind) => {
    if (!el || !vis(el)) return;
    const t = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 80 || seen.has(kind + t)) return;
    if (kind === 'button' && (/^\\d+$/.test(t) || /^(dropdown trigger|previous|next|first|last)/i.test(t))) return;
    seen.add(kind + t);
    el.setAttribute('data-qa-check', String(out.length));
    out.push({n: String(out.length), kind, text: t});
  };
  if (!scopeSel) {
    scope.querySelectorAll('h1, h2, h3, .page-title, app-title h1, app-title h2').forEach(e => add(e, 'heading'));
    scope.querySelectorAll('.p-breadcrumb li a, .breadcrumb li, nav[aria-label=breadcrumb] li').forEach(e => add(e, 'breadcrumb'));
    scope.querySelectorAll('table thead th').forEach(e => add(e, 'column'));
    scope.querySelectorAll('button, [role=button]').forEach(e => { if (!e.closest('table tbody')) add(e, 'button'); });
    const row = scope.querySelector('table tbody tr');
    if (row) row.querySelectorAll('button[title], button[aria-label]').forEach(e => add(e, 'row-action'));
  } else {
    scope.querySelectorAll('.p-dialog-title, .modal-title, h2, h3').forEach(e => add(e, 'heading'));
    scope.querySelectorAll('label, .field-label').forEach(e => add(e, 'label'));
    scope.querySelectorAll('button').forEach(e => add(e, 'button'));
  }
  return out;
}"""

MESSAGES_JS = """(scopeSel) => {
  document.querySelectorAll('[data-qa-msg]').forEach(e => e.removeAttribute('data-qa-msg'));
  const scope = (scopeSel ? [...document.querySelectorAll(scopeSel)].filter(e => e.getBoundingClientRect().width > 0).pop() : null) || document.body;
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const sel = '.p-error, small.p-error, .invalid-feedback, .error-message, .field-error, .text-danger, mat-error, .ant-form-item-explain-error, .p-message-error';
  const out = []; const seen = new Set();
  const worded = [...scope.querySelectorAll('small, span, div, p')].filter(el =>
    !el.children.length && /\\b(is required|required|invalid|must|cannot|please (enter|select))\\b/i.test(el.innerText || '')
    && (el.innerText || '').length < 120 && !el.closest('label'));
  [...scope.querySelectorAll(sel), ...worded, ...document.querySelectorAll('p-toast .p-toast-message, .p-toast-message')].forEach(el => {
    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
    if (t && vis(el) && !seen.has(t) && t.length < 160) { seen.add(t); el.setAttribute('data-qa-msg', String(out.length)); out.push(t); }
  });
  return out;
}"""

OVERLAY_OPTIONS_JS = """() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return [...document.querySelectorAll('.p-select-overlay [role=option], .p-dropdown-panel [role=option], .p-multiselect-overlay [role=option], .p-autocomplete-overlay [role=option], .cdk-overlay-pane [role=option], .ant-select-dropdown [role=option]')]
    .filter(vis).map(o => (o.getAttribute('aria-label') || o.innerText || '').trim()).filter(Boolean);
}"""


# ──────────────────────────────────────────────────────────────────────
# Recording session per browser context
# ──────────────────────────────────────────────────────────────────────
class Session:
    """The recorder's event sink for one context: same rules as main's handler."""

    def __init__(self) -> None:
        self.actions: list[dict] = []
        self.active = False
        self.page = None
        self.shot_dir = None
        self.shot_url = None
        self._shots = 0

    async def on_event(self, event: dict) -> None:
        """The recorder's callback: record the step, then photograph the page.

        Every step of a dashboard recording carries a screenshot; without one
        here the explorer's flows showed "no screenshot" on every card.
        """
        before = len(self.actions)
        self.handle(event)
        if len(self.actions) > before:
            await self.snap(self.actions[-1])

    async def snap(self, action: dict) -> None:
        if not (self.page and self.shot_dir and self.shot_url):
            return
        self._shots += 1
        name = f"step_{self._shots}.jpg"
        try:
            await self.page.screenshot(path=str(self.shot_dir / name), type="jpeg", quality=60)
            action["screenshot"] = self.shot_url(name)
        except Exception:
            pass

    def handle(self, event: dict) -> None:
        if not self.active or not isinstance(event, dict):
            return
        if event.get("action") == "__EFFECT__":
            for candidate in reversed(self.actions):
                if candidate.get("selector") == event.get("selector"):
                    candidate["effect"] = event.get("effect") or {}
                    return
            if self.actions:
                self.actions[-1]["effect"] = event.get("effect") or {}
            return
        selector = event.get("selector")
        if selector and event.get("required") is not True:
            if any(a.get("selector") == selector and a.get("required") is True for a in self.actions):
                event["required"] = True
        event.setdefault("screenshot", None)
        self.actions.append(event)

    def start(self, page=None, shot_dir=None, shot_url=None) -> None:
        self.actions, self.active = [], True
        self.page, self.shot_dir, self.shot_url, self._shots = page, shot_dir, shot_url, 0
        if shot_dir is not None:
            shot_dir.mkdir(parents=True, exist_ok=True)
            for old in shot_dir.glob("step_*.*"):     # last exploration's step images
                try:
                    old.unlink()
                except OSError:
                    pass

    def stop(self) -> list[dict]:
        self.active = False
        return self.actions


@dataclass
class ScreenResult:
    flow_name: str
    menu_path: list[str]
    url: str = ""
    status: str = "pending"          # explored | kept | skipped | error
    steps: int = 0
    form_title: str = ""
    fields: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unexpected_save: bool = False


#: Ids a framework generates per page load (PrimeNG pn_id_12, Angular Material
#: mat-input-3 ...). The recorder demotes them, but takes one when nothing else
#: exists on the element itself; a stable alternative from the list is better.
UNSTABLE_ID = re.compile(r"^#(pn_id_|p-|mat-|cdk-|ng-|ui-id|react-|radix-)", re.I)


def tidy(actions: list[dict]) -> list[dict]:
    """Drop the Tab the explorer pressed to commit a value (KEYPRESS then TYPE),
    and prefer a stable selector over a generated id."""
    for action in actions:
        sel = str(action.get("selector") or "")
        if UNSTABLE_ID.match(sel):
            stable = [x for x in (action.get("selectors") or []) if not UNSTABLE_ID.match(str(x))]
            if stable:
                action["selector"] = stable[0]
                action["selectors"] = stable + [sel]
    out = []
    for i, action in enumerate(actions):
        nxt = actions[i + 1] if i + 1 < len(actions) else {}
        if (action.get("action") == "KEYPRESS" and action.get("value") == "Tab"
                and nxt.get("action") == "TYPE" and nxt.get("selector") == action.get("selector")):
            continue
        out.append(action)
    return out


def fill_screenshots(actions: list[dict]) -> list[dict]:
    """Steps the explorer inserted itself take the next (else previous) picture."""
    shots = [a.get("screenshot") for a in actions]
    for i, action in enumerate(actions):
        if action.get("screenshot"):
            continue
        later = next((x for x in shots[i + 1:] if x), None)
        earlier = next((x for x in reversed(shots[:i]) if x), None)
        action["screenshot"] = later or earlier
    return actions


def flow_name_for(path: list[str]) -> str:
    clean = [re.sub(r'[<>:"/\\|?*]+', "-", p).strip(" .-") or "Screen" for p in path]
    return "/".join(clean)


def sample_value(f: dict) -> str | None:
    """A plausible value for a field, or None when it must not be typed."""
    text = f"{f.get('label', '')} {f.get('name', '')} {f.get('placeholder', '')}".lower()
    kind = f.get("kind")
    if kind in ("password", "file", "toggle", "date") or re.search(r"pass(word)?|otp|captcha|pin\b", text):
        return None
    if kind == "email" or "email" in text:
        return "qa.auto@example.com"
    if kind == "phone" or re.search(r"phone|mobile|contact no|whatsapp", text):
        return "9876543210"
    if kind == "number":
        return "10"
    if re.search(r"pin ?code|zip|postal", text):
        return "560001"
    if "gst" in text:
        return "29ABCDE1234F1Z5"
    if re.search(r"\bpan\b", text):
        return "ABCDE1234F"
    if "ifsc" in text:
        return "SBIN0001234"
    if re.search(r"account (no|number)|acc(ount)? ?no", text):
        return "123456789012"
    if re.search(r"url|website", text):
        return "https://example.com"
    if re.search(r"\bcode\b", text):
        return "QAAUTO01"
    label = re.sub(r"[*:]", "", f.get("label") or "value").strip()
    return f"QA-AUTO {label}"[:40]


# ──────────────────────────────────────────────────────────────────────
# Explorer
# ──────────────────────────────────────────────────────────────────────
class Explorer:
    def __init__(self, project: str, login_url: str, *, headless: bool = True,
                 only: str = "", workers: int = 3, empty_save: bool = True,
                 skip_existing: bool = False, emit: Emit | None = None) -> None:
        self.project = project
        self.login_url = login_url
        self.headless = headless
        self.only = only.lower().strip()
        self.workers = max(1, workers)
        self.empty_save = empty_save
        self.skip_existing = skip_existing
        self._emit = emit
        self.results: list[ScreenResult] = []

    async def log(self, message: str) -> None:
        log.info(message)
        if self._emit:
            out = self._emit(message)
            if asyncio.iscoroutine(out):
                await out

    def already_explored(self, path: list[str]) -> bool:
        meta = storage.read_json(config.flow_dir(self.project, flow_name_for(path)) / "flow.json", {})
        return bool(meta) and meta.get("source") == "explorer"

    # ── login ────────────────────────────────────────────────────────
    async def login(self, page: Page) -> None:
        creds = credential_store.load_stored(self.project)
        user, pwd = creds.get("username"), creds.get("password")
        if not user or not pwd:
            raise RuntimeError(f"No stored login for project {self.project!r}. Record the "
                               "login flow once, or set it in Settings.")
        await page.goto(self.login_url, wait_until="domcontentloaded", timeout=60000)
        # A single-page app draws its login form after the page has "loaded";
        # wait for the form, or for the menu if a session is already signed in.
        try:
            await page.wait_for_selector(f"input[type=password], {NAV_CONTAINER}",
                                         state="visible", timeout=30000)
        except Exception:
            pass
        await pacing.settle(page, 8000)
        password = page.locator("input[type=password]").first
        if not await password.count():
            await self.log("No login form found — assuming the session is already signed in.")
            return
        user_box = page.locator("input:not([type=password]):not([type=hidden]):not([type=checkbox])").first
        await user_box.fill(user)
        await password.fill(pwd)
        before = page.url
        submit = page.locator("button[type=submit], button:has-text('Login'), "
                              "button:has-text('Log in'), button:has-text('Sign in')").first
        await submit.click()
        for _ in range(60):
            if page.url != before:
                break
            await page.wait_for_timeout(250)
        await pacing.settle(page, 8000)
        if page.url == before:
            raise RuntimeError("Login did not leave the login page — check the stored credentials.")
        try:
            await page.wait_for_selector(NAV_CONTAINER, state="visible", timeout=30000)
        except Exception:
            pass
        await pacing.settle(page, 8000)
        await self.log(f"Signed in as {user}.")

    # ── menu ─────────────────────────────────────────────────────────
    async def discover(self, page: Page) -> list[tuple[list[str], str]]:
        """Every menu leaf as (path, url), by clicking each one."""
        items = await page.evaluate(MENU_JS, NAV_CONTAINER)
        await self.log(f"Menu: {len(items)} candidate screens.")
        found: list[tuple[list[str], str]] = []
        seen_urls: set[str] = set()
        for item in items:
            path = item["path"]
            if self.only and not " / ".join(path).lower().startswith(self.only):
                continue
            # Opening a menu item only navigates, so "Dispatch" or "Approvals"
            # screens are safe to visit; their buttons are guarded inside. Only
            # an item that would end the session is skipped.
            if LOGOUT.search(" ".join(path)):
                continue
            before = page.url
            # The app redraws its menu after each navigation, which drops the
            # markers; re-read it and find this item again by its path.
            current = await page.evaluate(MENU_JS, NAV_CONTAINER)
            match = next((c for c in current if c["path"] == path), None)
            if not match:
                await self.log(f"  not found again in the menu: {' > '.join(path)}")
                continue
            try:
                await page.evaluate("id => { const el = document.querySelector(`[data-qa-menu=\"${id}\"]`);"
                                    " if (el) (el.querySelector('a') || el).click(); }", match["id"])
            except Exception:
                continue
            await pacing.settle(page, 6000)
            url = page.url
            if url != before and url not in seen_urls and "/auth/" not in url:
                seen_urls.add(url)
                found.append((path, url))
        await self.log(f"Menu: {len(found)} screens with their own address.")
        return found

    # ── one screen ───────────────────────────────────────────────────
    async def check(self, page: Page, attr: str, n: str) -> None:
        """A check step: a click the recorder swallows (assert mode)."""
        await page.evaluate(
            "([attr, n]) => { const el = document.querySelector(`[${attr}=\"${n}\"]`);"
            " if (el) el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})); }",
            [attr, n])

    async def assert_mode(self, page: Page, on: bool) -> None:
        await page.evaluate(f"window.__qaAssertMode = {'true' if on else 'false'};")

    async def screen(self, page: Page, session: Session, path: list[str], url: str) -> ScreenResult:
        name = flow_name_for(path)
        result = ScreenResult(flow_name=name, menu_path=path, url=url)
        target = config.flow_dir(self.project, name)
        existing = storage.read_json(target / "flow.json", {}) or {}
        if (target / "actions.json").exists() and existing.get("source") != "explorer":
            result.status = "kept"
            result.notes.append("A recording made by a person already exists; it was kept.")
            return result

        started = time.time()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await pacing.settle(page, 10000)
        if "/auth/" in page.url:
            result.status, result.notes = "error", ["Redirected to login."]
            return result

        session.start(page=page, shot_dir=target / "screenshots",
                      shot_url=lambda file: config.static_url(self.project, name, "screenshots", file))
        session.actions.append({"action": "NAVIGATE", "selector": url, "selectors": [url],
                                "tag": "", "text": "", "value": url, "screenshot": None})
        await session.snap(session.actions[-1])
        allow_empty_save = self.empty_save and path[0].lower().startswith(EMPTY_SAVE_GROUPS)

        # List page: checks only.
        targets = await page.evaluate(CHECK_TARGETS_JS, "")
        await self.assert_mode(page, True)
        for t in targets:
            await self.check(page, "data-qa-check", t["n"])
        await self.assert_mode(page, False)
        result.columns = [t["text"] for t in targets if t["kind"] == "column"]
        result.buttons = [t["text"] for t in targets if t["kind"] in ("button", "row-action")]
        await page.wait_for_timeout(300)
        await self._shot(page, target, "explore_list.png")

        # The Add form, when the screen has one.
        add = page.locator("button, a.p-button, [role=button]").filter(has_text=ADD_BUTTON).first
        if await add.count() and await add.is_visible():
            label = (await add.inner_text()).strip()
            if DANGER.search(label):
                result.notes.append(f"'{label}' looks unsafe; not opened.")
            else:
                try:
                    await add.click()
                    await page.wait_for_timeout(EFFECT_WAIT_MS)
                    await pacing.settle(page, 6000)
                    in_dialog = await page.evaluate(FORM_ROOT_JS) == "dialog"
                    scope = ".p-dialog, [role=dialog], .modal.show, mat-dialog-container" if in_dialog else ""
                    await self.form(page, session, scope, result, allow_empty_save, target)
                    if in_dialog:
                        await self.close_form(page, scope)
                    elif page.url != url:
                        await page.go_back()
                        await pacing.settle(page, 6000)
                except Exception as err:
                    # Keep what was recorded; say what could not be done.
                    result.notes.append(f"Form not fully explored: {str(err).splitlines()[0][:160]}")
                    await self.log(f"  {name}: form step failed — {str(err).splitlines()[0][:120]}")
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
        else:
            fields = await page.evaluate(FIELDS_JS, "")
            if len(fields) >= 3:
                result.notes.append("Screen has input fields (search/filter boxes or a form) and no Add button; their labels were documented, nothing was filled.")
                await self.assert_mode(page, True)
                for t in (await page.evaluate(CHECK_TARGETS_JS, "app-pages, main"))[:MAX_FIELDS]:
                    if t["kind"] == "label":
                        await self.check(page, "data-qa-check", t["n"])
                await self.assert_mode(page, False)
                result.fields = fields[:MAX_FIELDS]

        await page.wait_for_timeout(EFFECT_WAIT_MS)
        actions = fill_screenshots(tidy(session.stop()))
        result.steps = len(actions)
        storage.write_json(target / "actions.json", actions)
        storage.write_json(target / "flow.json", {
            "schema_version": 1,
            "flow_name": name,
            "target_url": url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": int(time.time() - started),
            "step_count": len(actions),
            "source": "explorer",
            "menu_path": path,
        })
        result.status = "explored"
        return result

    async def form(self, page: Page, session: "Session", scope: str, result: ScreenResult,
                   allow_empty_save: bool, target) -> None:
        title = page.locator(".p-dialog-title, .modal-title").last
        if scope and await title.count():
            result.form_title = (await title.inner_text()).strip()

        await self.assert_mode(page, True)
        for t in await page.evaluate(CHECK_TARGETS_JS, scope or "app-pages, main"):
            if t["kind"] in ("heading", "label"):
                await self.check(page, "data-qa-check", t["n"])
        await self.assert_mode(page, False)

        fields = await page.evaluate(FIELDS_JS, scope)
        if len(fields) > MAX_FIELDS:
            result.notes.append(f"{len(fields)} fields; the first {MAX_FIELDS} were documented.")
            fields = fields[:MAX_FIELDS]

        # 1. Empty Save: the application's own required-field messages.
        required_empty = [f for f in fields if f["required"] and f["empty"]
                          and not f["disabled"] and not f["readonly"]]
        if scope and allow_empty_save and required_empty:
            save = page.locator(scope).locator("button").filter(has_text=SAVE_BUTTON).last
            if await save.count() and await save.is_visible() and not await save.is_enabled():
                # The form gates submission instead of showing messages: Save
                # stays disabled until the mandatory fields are filled. That is
                # the rule to test, so record it as a check.
                text = (await save.inner_text()).strip() or "Save"
                session.actions.append({
                    "action": "VERIFY_DISABLED", "selector": f'button:has-text("{text}")',
                    "selectors": [f'button:has-text("{text}")'], "tag": "button", "text": text,
                    "value": "", "screenshot": None, "asserted": True,
                    "synthetic": "save disabled while mandatory fields are empty"})
                result.notes.append(f"'{text}' stays disabled until the mandatory fields are filled.")
            elif await save.count() and await save.is_visible():
                await save.click()
                await page.wait_for_timeout(EFFECT_WAIT_MS)
                still_open = await page.evaluate(FORM_ROOT_JS) == "dialog"
                messages = await page.evaluate(MESSAGES_JS, scope)
                effect = next((a.get("effect") for a in reversed(session.actions)
                               if a.get("action") == "CLICK" and a.get("effect")), None) or {}
                for m in effect.get("messages", []):
                    text = (m.get("text") or "").strip()
                    if text and text not in messages and len(text) < 160 \
                            and not SUCCESS_TEXT.search(text):
                        messages.append(text)
                if not still_open or any(SUCCESS_TEXT.search(m) for m in messages):
                    result.unexpected_save = True
                    result.notes.append("EMPTY SAVE WAS ACCEPTED — check the application "
                                        "for a blank record.")
                    await self.log(f"  !! {result.flow_name}: an empty Save was accepted")
                    return
                result.validation = messages
                await self.assert_mode(page, True)
                for i in range(len(messages)):
                    await self.check(page, "data-qa-msg", str(i))
                await self.assert_mode(page, False)
        elif scope and required_empty and not allow_empty_save:
            result.notes.append("Transaction form: no Save clicked, not even empty.")

        # 2. Every field: fill, open, choose. New fields that appear after a
        #    choice (Category loading after Type) are picked up on the next pass.
        collected: dict[tuple, dict] = {}
        for _ in range(3):
            fields = (await page.evaluate(FIELDS_JS, scope))[:MAX_FIELDS]
            pending = [f for f in fields if (f["label"], f["name"]) not in collected]
            if not pending:
                break
            for f in pending:
                collected[(f["label"], f["name"])] = f
                if f["disabled"] or f["readonly"]:
                    continue
                handle = page.locator(f'[data-qa-field="{f["n"]}"]').first
                precise = self.precise_selector(f)
                start = len(session.actions)
                try:
                    if f["kind"] in ("dropdown", "multiselect", "autocomplete"):
                        f["options"] = await self.choose(page, handle, f["kind"])
                        # The recorder sees the option click but not the click
                        # that opened the list; without it a replay clicks an
                        # option that is not on screen yet.
                        opened = precise or (f'[aria-label="{f["label"].rstrip(" *")}"]'
                                             if f.get("label") else "")
                        if opened and f["options"]:
                            session.actions.insert(start, {
                                "action": "CLICK", "selector": opened, "selectors": [opened],
                                "tag": f["kind"], "text": "", "value": "",
                                "label": f.get("label", ""), "required": bool(f.get("required")),
                                "screenshot": None, "synthetic": "open dropdown"})
                    else:
                        value = sample_value(f)
                        if value is None:
                            continue
                        box = handle.locator("input").first if await handle.locator("input").count() else handle
                        await box.click()
                        await box.fill("")
                        await box.press_sequentially(value, delay=10)
                        await box.press("Tab")
                        f["sample"] = value
                        await page.wait_for_timeout(150)
                        self.pin_selector(session.actions, start, precise)
                except Exception as err:   # one stubborn widget must not stop the screen
                    f["note"] = f"not filled: {str(err).splitlines()[0][:80]}"
        result.fields = list(collected.values())
        await self._shot(page, target, "explore_form.png")

    @staticmethod
    def precise_selector(f: dict) -> str:
        """The field by its form name -- unique, unlike a shared placeholder."""
        name = f.get("name")
        if not name:
            return ""
        if f.get("kind") in ("number", "date", "autocomplete"):
            return f'[formcontrolname="{name}"] input'
        if f.get("kind") in ("dropdown", "multiselect"):
            return f'[formcontrolname="{name}"]'
        return f'[formcontrolname="{name}"]'

    @staticmethod
    def pin_selector(actions: list[dict], start: int, precise: str) -> None:
        """Make the field's own name the primary selector of what was just recorded.

        The recorder picks a placeholder when nothing better is on the inner
        input, and Liberty gives eight number fields the same "0.00" -- a replay
        would type into the first one. The recorder's choice stays as fallback.
        """
        if not precise:
            return
        for action in actions[start:]:
            if action.get("action") in ("TYPE", "CLICK", "KEYPRESS", "SELECT", "CLEAR") \
                    and not str(action.get("selector", "")).startswith(("[aria-label", "li")):
                old = action.get("selector") or ""
                rest = [x for x in (action.get("selectors") or []) if x not in (precise, old)]
                action["selector"] = precise
                action["selectors"] = [precise] + ([old] if old else []) + rest

    async def choose(self, page: Page, handle, kind: str) -> list[str]:
        if await handle.evaluate("el => el.tagName") == "SELECT":
            options = await handle.evaluate("el => [...el.options].map(o => o.text.trim())")
            if len(options) > 1:
                await handle.select_option(index=1)
            return options[:MAX_OPTIONS]
        await handle.click()
        await page.wait_for_timeout(EFFECT_WAIT_MS)
        options = (await page.evaluate(OVERLAY_OPTIONS_JS))[:MAX_OPTIONS]
        option = page.locator(".p-select-overlay [role=option], .p-dropdown-panel [role=option], "
                              ".p-multiselect-overlay [role=option], .cdk-overlay-pane [role=option], "
                              ".ant-select-dropdown [role=option]").first
        if options and await option.count():
            await option.click()
            await page.wait_for_timeout(EFFECT_WAIT_MS if kind != "multiselect" else 600)
            if kind == "multiselect":
                await page.keyboard.press("Escape")
        else:
            await page.keyboard.press("Escape")
        await pacing.settle(page, 4000)
        return options

    async def close_form(self, page: Page, scope: str) -> None:
        cancel = page.locator(scope).locator("button").filter(has_text=CANCEL_BUTTON).last
        if await cancel.count() and await cancel.is_visible():
            await cancel.click()
        else:
            close = page.locator(f"{scope} .p-dialog-header-close, {scope} [aria-label=Close]").last
            if await close.count():
                await close.click()
            else:
                await page.keyboard.press("Escape")
        await page.wait_for_timeout(EFFECT_WAIT_MS)
        if await page.evaluate(FORM_ROOT_JS) == "dialog":
            await page.keyboard.press("Escape")

    async def _shot(self, page: Page, target, name: str) -> None:
        try:
            (target / "screenshots").mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(target / "screenshots" / name))
        except Exception:
            pass

    # ── run ──────────────────────────────────────────────────────────
    async def new_context(self, browser, state_file):
        args: dict[str, Any] = {"ignore_https_errors": True, "accept_downloads": False}
        if self.headless:
            args["viewport"] = {"width": 1600, "height": 900}
        else:
            args["no_viewport"] = True
        if state_file:
            args["storage_state"] = state_file
        context = await browser.new_context(**args)
        return context

    async def wire(self, context, session: Session) -> Page:
        await context.expose_function("recordPlaywrightEvent", session.on_event)
        await context.add_init_script(recorder.JS_RECORDER_SCRIPT)
        page = await context.new_page()
        page.set_default_timeout(10000)
        await pacing.track(page)
        return page

    async def run(self) -> dict:
        started = time.time()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless, args=screens.launch_args(None, 1, self.headless))
            first = await self.new_context(browser, None)
            page = await first.new_page()
            await pacing.track(page)
            await self.login(page)
            state_file = str(config.TEMP_DIR / f"explore_{self.project}.json")
            config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
            await first.storage_state(path=state_file)
            screens_found = await self.discover(page)
            await first.close()

            queue: asyncio.Queue = asyncio.Queue()
            for item in screens_found:
                queue.put_nowait(item)

            async def fresh():
                context = await self.new_context(browser, state_file)
                session = Session()
                return context, session, await self.wire(context, session)

            async def worker(slot: int) -> None:
                context, session, page = await fresh()
                try:
                    while True:
                        try:
                            path, url = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        if self.skip_existing and self.already_explored(path):
                            continue
                        # A screen can close its own tab or crash the renderer;
                        # one bad screen must not take the rest of the run down.
                        if page.is_closed():
                            try:
                                await context.close()
                            except Exception:
                                pass
                            context, session, page = await fresh()
                        try:
                            res = await asyncio.wait_for(self.screen(page, session, path, url), 240)
                        except Exception as err:
                            log.warning("Screen %s failed: %s", " > ".join(path),
                                        traceback.format_exc(limit=6))
                            session.stop()
                            res = ScreenResult(flow_name_for(path), path, url, status="error",
                                               notes=[str(err).splitlines()[0][:200]])
                            try:
                                await page.keyboard.press("Escape")
                            except Exception:
                                pass
                        self.results.append(res)
                        await self.log(f"  [{len(self.results)}/{len(screens_found)}] {res.status:<8} "
                                       f"{res.flow_name}  ({res.steps} steps, {len(res.fields)} fields)")
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass

            outcomes = await asyncio.gather(*(worker(i) for i in range(self.workers)),
                                            return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    tb = "".join(traceback.format_exception(outcome, limit=6))
                    await self.log(f"A worker stopped: {outcome!r} {tb}")
            try:
                await browser.close()
            except Exception:
                pass

        summary = self.write_summary(time.time() - started)
        await self.log(f"Done: {summary['explored']} explored, {summary['kept']} kept, "
                       f"{summary['errors']} errors in {summary['seconds']} s.")
        return summary

    def write_summary(self, seconds: float) -> dict:
        self.results.sort(key=lambda r: r.flow_name.lower())
        out_dir = config.project_dir(self.project) / "explorer"
        rows = []
        for r in self.results:
            rows.append({
                "flow": r.flow_name, "menu": " > ".join(r.menu_path), "url": r.url,
                "status": r.status, "steps": r.steps, "form": r.form_title,
                "fields": [{k: f.get(k) for k in ("label", "name", "kind", "required", "readonly",
                                                   "disabled", "options", "sample", "note") if f.get(k) not in (None, "", [])}
                           for f in r.fields],
                "columns": r.columns, "buttons": r.buttons, "validation": r.validation,
                "unexpected_save": r.unexpected_save, "notes": r.notes,
            })
        # Screens not visited this time (--skip-existing, --only) keep their
        # earlier rows, so the summary always describes the whole application.
        previous = storage.read_json(out_dir / "summary.json", {}) or {}
        this_run = {row["flow"] for row in rows}
        rows += [row for row in previous.get("screens", []) if row.get("flow") not in this_run]
        rows.sort(key=lambda r: r["flow"].lower())
        summary = {
            "project": self.project, "login_url": self.login_url,
            "explored_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": int(seconds),
            "explored": sum(r["status"] == "explored" for r in rows),
            "kept": sum(r["status"] == "kept" for r in rows),
            "errors": sum(r["status"] == "error" for r in rows),
            "unexpected_saves": [r["flow"] for r in rows if r.get("unexpected_save")],
            "screens": rows,
        }
        storage.write_json(out_dir / "summary.json", summary)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Module", "Menu path", "URL", "Status", "Form", "Field", "Type",
                         "Mandatory", "Read-only", "Options", "Validation messages", "Notes"])
        for row in rows:
            fields = row["fields"] or [{}]
            for i, f in enumerate(fields):
                writer.writerow([
                    row["flow"], row["menu"], row["url"], row["status"], row["form"],
                    f.get("label", ""), f.get("kind", ""), "yes" if f.get("required") else "",
                    "yes" if f.get("readonly") else "", " | ".join(f.get("options") or [])[:300],
                    " | ".join(row["validation"]) if i == 0 else "",
                    " ".join(row["notes"]) if i == 0 else "",
                ])
        storage.write_text(out_dir / "summary.csv", buf.getvalue())
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--url", required=True, help="the application's login page")
    parser.add_argument("--only", default="", help="menu group prefix, e.g. Master")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-empty-save", action="store_true")
    parser.add_argument("--skip-existing", action="store_true",
                        help="leave screens explored earlier alone (re-run only the rest)")
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    explorer = Explorer(args.project, args.url, headless=not args.headed, only=args.only,
                        workers=args.workers, empty_save=not args.no_empty_save,
                        skip_existing=args.skip_existing,
                        emit=None)  # the logger already prints to the console
    summary = asyncio.run(explorer.run())
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
