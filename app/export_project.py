"""Export a recorded project as a standalone Playwright POM test project.

A separate, **read-only** feature. It reads a project's flows and returns a set of
files to zip — it never writes to a flow folder, never touches ``test_cases.json``,
and nothing in the executor or verdict engine is involved. Deleting the export or
never using it changes nothing about how the platform runs.

The output follows the structure a real Playwright suite uses::

    pages/       one class per module, locators in the constructor, methods are
                 the actions a user would take
    tests/       one spec per module; specs never touch raw locators
    testdata/    JSON the specs read, split into static and per-run values
    playwright.config.js  package.json  README.md  .env.example

Everything is derived deterministically from what the platform already holds —
the recorded selectors, the learned field inventory (label, type, required) and
the generated scenarios. **No model is called**, so the same project always
produces the same code.

The generator also encodes the house rules a real suite learns the hard way:

* a control with more than one recorded selector becomes ``a.or(b)`` rather than
  a single brittle guess — the fix for locators that match two elements, and for
  fields that only render under some conditions;
* nothing is counted before it is waited for, because ``.count()`` does not retry
  the way ``expect()`` does;
* a click that makes its own element disappear is given a bounded timeout and
  then asserted on the *resulting state*, never on the click settling;
* long multi-screen flows get an explicit ``test.setTimeout()``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config

log = config.get_logger("export")

INDENT = "    "

#: Steps that only exist inside the platform's engine. They have no faithful
#: one-line Playwright equivalent, so they are emitted as a TODO comment naming
#: what the platform did — an honest gap beats code that silently checks nothing.
ENGINE_ONLY_ACTIONS = {
    "SWITCH_ROLE", "API_REQUEST", "VERIFY_API", "WAIT_FOR_STATE",
    "VERIFY_COLUMN_NOT_EMPTY", "VERIFY_NO_NOOP_ROWS", "VERIFY_STATE",
}


# ──────────────────────────────────────────────────────────────────────
# Naming
# ──────────────────────────────────────────────────────────────────────
def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^A-Za-z0-9]+", str(text or "")) if w]


def pascal(text: str, fallback: str = "Item") -> str:
    parts = _words(text)
    return "".join(p[:1].upper() + p[1:] for p in parts) or fallback


def camel(text: str, fallback: str = "field") -> str:
    name = pascal(text, fallback)
    return name[:1].lower() + name[1:]


def kebab(text: str, fallback: str = "module") -> str:
    parts = [w.lower() for w in _words(text)]
    return "-".join(parts) or fallback


def js_string(value: Any) -> str:
    """A JS single-quoted literal. json.dumps handles the escaping correctly."""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────
# Locators
# ──────────────────────────────────────────────────────────────────────
def locator_expression(selector: str, alternates: list[str] | None = None) -> str:
    """``page.locator(a)`` — or ``a.or(b)`` when the recording captured fallbacks.

    A single recorded selector is one literal spelling of one control. Where the
    recorder captured alternatives, ``.or()`` is Playwright's own way of saying
    "whichever of these is present", which is what makes the page object survive
    a conditionally-rendered field or a second element with the same text.
    """
    primary = (selector or "").strip()
    if not primary:
        return "page.locator('body')"
    chain = f"page.locator({js_string(primary)})"
    for extra in (alternates or [])[:2]:
        extra = (extra or "").strip()
        if extra and extra != primary:
            chain += f".or(page.locator({js_string(extra)}))"
    return chain


def field_property(field: dict) -> tuple[str, str]:
    """``(propertyName, locatorExpression)`` for one inventoried field."""
    label = str(field.get("label") or "").strip().rstrip("*").strip()
    selector = str(field.get("selector") or "").strip()
    name = camel(label or selector, "field")
    return name, locator_expression(selector, field.get("selectors"))


# ──────────────────────────────────────────────────────────────────────
# Steps → page-object methods and spec bodies
# ──────────────────────────────────────────────────────────────────────
def _property_for_selector(selector: str, index: dict[str, str]) -> str:
    return index.get((selector or "").strip(), "")


def build_locator_index(fields: list[dict], actions: list[dict]) -> dict[str, str]:
    """selector → page-object property name, for everything worth naming."""
    index: dict[str, str] = {}
    for field in fields or []:
        selector = str(field.get("selector") or "").strip()
        if selector and selector not in index:
            index[selector] = field_property(field)[0]
    for act in actions or []:
        selector = str(act.get("selector") or "").strip()
        if not selector or selector in index:
            continue
        if selector.startswith(("http://", "https://")):
            continue
        label = act.get("text") or act.get("label") or ""
        action = str(act.get("action") or "").upper()
        prefix = "" if action in ("TYPE", "SELECT") else "btn"
        name = camel(f"{prefix} {label}" if label else selector, "control")
        index[selector] = name
    return index


def step_to_js(step: dict, index: dict[str, str], *,
               next_asserts: bool = False, data_var: str = "") -> list[str]:
    """One platform step as one or more lines of Playwright JS.

    ``spec=True`` emits calls against the page object (``await module.x``);
    otherwise the lines are the body of a page-object method (``await this.x``).
    """
    action = str(step.get("action") or "").upper()
    selector = str(step.get("selector") or "").strip()
    value = step.get("value", "")
    prop = _property_for_selector(selector, index)
    target = f"this.{prop}" if prop else f"this.page.locator({js_string(selector)})"

    if action in ENGINE_ONLY_ACTIONS:
        return [f"// TODO ({action}): the platform verified this internally; "
                f"no direct Playwright equivalent. selector={selector or 'n/a'} "
                f"value={value or 'n/a'}"]

    if action == "NAVIGATE":
        return [f"await this.page.goto({js_string(value or selector)});"]
    if action in ("TYPE", "CLEAR"):
        literal = js_string("" if action == "CLEAR" else value)
        if data_var and prop and action == "TYPE":
            return [f"await {target}.fill(String({data_var}.{prop} ?? {literal}));"]
        return [f"await {target}.fill({literal});"]
    if action == "SELECT":
        literal = js_string(value)
        if data_var and prop:
            return [f"await {target}.selectOption({{ label: "
                    f"String({data_var}.{prop} ?? {literal}) }});"]
        return [f"await {target}.selectOption({{ label: {literal} }});"]
    if action in ("CHECK", "UNCHECK"):
        return [f"await {target}.{'check' if action == 'CHECK' else 'uncheck'}();"]
    if action == "HOVER":
        return [f"await {target}.hover();"]
    if action == "SCROLL_TO":
        return [f"await {target}.scrollIntoViewIfNeeded();"]
    if action == "UPLOAD":
        return [f"await {target}.setInputFiles({js_string(value)});"]
    if action == "KEYPRESS":
        return [f"await this.page.keyboard.press({js_string(value or 'Enter')});"]
    if action in ("WAIT", "WAIT_FOR"):
        if action == "WAIT" and str(value).strip().isdigit():
            return [f"await this.page.waitForTimeout({int(str(value).strip())});"]
        return [f"await {target}.waitFor({{ state: 'visible' }});"]
    if action == "SCREENSHOT":
        return [f"await this.page.screenshot({{ path: {js_string(value or 'step.png')} }});"]
    if action == "STORE":
        return [f"const {camel(str(value), 'captured')} = "
                f"(await {target}.inputValue().catch(() => {target}.innerText())).trim();",
                f"// captured for a later step / test-data file"]
    if action == "DIALOG":
        accept = "accept" if str(value).lower() != "dismiss" else "dismiss"
        return [f"this.page.once('dialog', (d) => d.{accept}());"]

    if action in ("CLICK", "DOUBLE_CLICK", "RIGHT_CLICK"):
        method = {"CLICK": "click", "DOUBLE_CLICK": "dblclick",
                  "RIGHT_CLICK": "click"}[action]
        if action == "RIGHT_CLICK":
            return [f"await {target}.click({{ button: 'right' }});"]
        if not next_asserts:
            # A plain click must fail loudly. Swallowing every click error would
            # let a genuinely broken step pass — the same "never turn ignorance
            # into a pass" mistake the platform's own engine avoids.
            return [f"await {target}.{method}();"]
        # This one is followed by an assertion on the resulting state, so an
        # Angular re-render that removes the button mid-click cannot hide a
        # failure: the next line still has to hold.
        return [f"await {target}.{method}({{ timeout: 10_000 }})",
                f"{INDENT}.catch(() => {{ /* the control may vanish on success; "
                f"the next assertion is the real check */ }});"]

    # ── Assertions ────────────────────────────────────────────────────
    prefix = "await expect("
    if action == "VERIFY_TEXT":
        return [f"{prefix}{target}).toContainText({js_string(value)});"]
    if action == "VERIFY_MESSAGE":
        return [f"{prefix}this.page.getByText({js_string(value)}, {{ exact: false }})"
                f".first()).toBeVisible();"]
    if action == "VERIFY_VISIBLE":
        return [f"{prefix}{target}.first()).toBeVisible();"]
    if action == "VERIFY_HIDDEN":
        return [f"{prefix}{target}.first()).toBeHidden();"]
    if action == "VERIFY_ENABLED":
        return [f"{prefix}{target}.first()).toBeEnabled();"]
    if action in ("VERIFY_DISABLED", "VERIFY_READONLY"):
        return [f"{prefix}{target}.first()).toBeDisabled();"]
    if action == "VERIFY_URL":
        return [f"{prefix}this.page).toHaveURL(new RegExp({js_string(value or selector)}));"]
    if action in ("VERIFY_VALUE", "ASSERT_VALUE"):
        return [f"{prefix}{target}).toHaveValue({js_string(value)});"]
    if action == "VERIFY_COUNT":
        digits = re.findall(r"\d+", str(value))
        return [
            f"await {target}.first().waitFor({{ state: 'visible' }});",
            f"{prefix}{target}).toHaveCount({digits[0] if digits else 1});",
        ]
    if action in ("VERIFY_ROW_CONTAINS", "VERIFY_TABLE_ROW"):
        return [
            f"await {target}.first().waitFor({{ state: 'visible' }});",
            f"{prefix}{target}.filter({{ hasText: {js_string(value)} }}).first())"
            f".toBeVisible();",
        ]
    if action == "VERIFY_ROW_ABSENT":
        return [f"{prefix}{target}.filter({{ hasText: {js_string(value)} }}))"
                f".toHaveCount(0);"]
    if action == "VERIFY_OPTION_PRESENT":
        return [f"{prefix}{target}.locator('option', {{ hasText: {js_string(value)} }}))"
                f".toHaveCount(1);"]
    if action == "VERIFY_OPTION_ABSENT":
        return [f"{prefix}{target}.locator('option', {{ hasText: {js_string(value)} }}))"
                f".toHaveCount(0);"]
    if action == "VERIFY_DOWNLOAD":
        return ["// the download is awaited by the caller via page.waitForEvent('download')"]
    if action == "VERIFY_NON_ZERO":
        return [
            f"await {target}.first().waitFor({{ state: 'visible' }});",
            f"expect(await {target}.count()).toBeGreaterThan(0);",
        ]

    return [f"// TODO ({action}): unmapped step. selector={selector or 'n/a'} "
            f"value={value or 'n/a'}"]


# ──────────────────────────────────────────────────────────────────────
# File builders
# ──────────────────────────────────────────────────────────────────────
def base_page_js(login_steps: list[dict], base_url: str) -> str:
    """``BasePage`` — login and the navigation every other page object extends."""
    lines = [
        "// Shared by every page object: login, and whatever navigation the",
        "// recording showed before the first form field was touched.",
        "const { expect } = require('@playwright/test');",
        "",
        "class BasePage {",
        f"{INDENT}constructor(page) {{",
        f"{INDENT*2}this.page = page;",
        f"{INDENT*2}this.baseUrl = process.env.BASE_URL || {js_string(base_url)};",
        f"{INDENT}}}",
        "",
        f"{INDENT}async gotoLogin() {{",
        f"{INDENT*2}await this.page.goto(this.baseUrl);",
        f"{INDENT}}}",
        "",
        f"{INDENT}async login(credentials) {{",
        f"{INDENT*2}await this.gotoLogin();",
    ]
    if login_steps:
        index = build_locator_index([], login_steps)
        for step in login_steps:
            action = str(step.get("action") or "").upper()
            selector = str(step.get("selector") or "").strip()
            if action == "TYPE":
                prop = _property_for_selector(selector, index)
                key = "username" if any(w in f"{selector}{step.get('label','')}".lower()
                                        for w in ("user", "email", "login")) else "password"
                lines.append(f"{INDENT*2}await this.page.locator({js_string(selector)})"
                             f".fill(credentials.{key});")
            elif action == "CLICK":
                lines.append(f"{INDENT*2}await this.page.locator({js_string(selector)})"
                             f".click();")
    else:
        lines.append(f"{INDENT*2}// No login flow was recorded — fill this in.")
    lines += [
        f"{INDENT*2}await this.page.waitForLoadState('networkidle').catch(() => {{}});",
        f"{INDENT}}}",
        "",
        f"{INDENT}// Every spec starts here.",
        f"{INDENT}async loginAndPrepare(credentials) {{",
        f"{INDENT*2}await this.login(credentials);",
        f"{INDENT}}}",
        "}",
        "",
        "module.exports = { BasePage };",
        "",
    ]
    return "\n".join(lines)


def page_object_js(flow_name: str, fields: list[dict], actions: list[dict],
                   scenarios: list[dict]) -> str:
    """One class per module: locators in the constructor, methods as user actions."""
    cls = pascal(flow_name, "Module") + "Page"
    index = build_locator_index(fields, actions)

    lines = [
        f"// Page object for the {flow_name} module.",
        "// Locators are declared here; spec files never touch a raw selector.",
        "const { expect } = require('@playwright/test');",
        "const { BasePage } = require('./BasePage');",
        "",
        f"class {cls} extends BasePage {{",
        f"{INDENT}constructor(page) {{",
        f"{INDENT*2}super(page);",
    ]

    seen: set[str] = set()
    for field in fields or []:
        name, expression = field_property(field)
        if name in seen:
            continue
        seen.add(name)
        label = str(field.get("label") or "").strip()
        note = f"    // {label}" if label else ""
        lines.append(f"{INDENT*2}this.{name} = {expression};{note}")
    for act in actions or []:
        selector = str(act.get("selector") or "").strip()
        name = index.get(selector, "")
        if not name or name in seen or selector.startswith(("http://", "https://")):
            continue
        seen.add(name)
        lines.append(f"{INDENT*2}this.{name} = "
                     f"{locator_expression(selector, act.get('selectors'))};")
    lines += [f"{INDENT}}}", ""]

    # fill(data) — every inventoried field, guarded so a conditionally rendered
    # one never breaks the run.
    if fields:
        lines += [
            f"{INDENT}// Only fills what the data object provides, and only when the",
            f"{INDENT}// field is actually on screen — several are conditional.",
            f"{INDENT}async fillForm(data) {{",
        ]
        for field in fields:
            name, _ = field_property(field)
            kind = str(field.get("kind") or "text").lower()
            method = "selectOption({ label: String(value) })" if kind in (
                "select", "dropdown") else "fill(String(value))"
            lines += [
                f"{INDENT*2}if (data.{name} !== undefined) {{",
                f"{INDENT*3}const value = data.{name};",
                f"{INDENT*3}if (await this.{name}.first().isVisible().catch(() => false)) {{",
                f"{INDENT*4}await this.{name}.first().{method};",
                f"{INDENT*3}}}",
                f"{INDENT*2}}}",
            ]
        lines += [f"{INDENT}}}", ""]

    for scenario in scenarios or []:
        method = camel(scenario.get("scenario_name") or "run", "runScenario")
        if method in seen:
            method = f"{method}Steps"
        seen.add(method)
        steps = scenario.get("steps") or []
        lines.append(f"{INDENT}// {scenario.get('scenario_name', '')}")
        if scenario.get("expected_result"):
            lines.append(f"{INDENT}// Expected: {scenario['expected_result']}")
        lines.append(f"{INDENT}async {method}(data = {{}}) {{")
        body: list[str] = []
        for position, step in enumerate(steps):
            following = steps[position + 1] if position + 1 < len(steps) else {}
            asserts_next = str(following.get("action") or "").upper().startswith(
                ("VERIFY", "ASSERT"))
            body.extend(step_to_js(step, index, next_asserts=asserts_next,
                                   data_var="data"))
        for line in body or ["// no steps recorded for this scenario"]:
            lines.append(f"{INDENT*2}{line}")
        lines += [f"{INDENT}}}", ""]

    lines += ["}", "", f"module.exports = {{ {cls} }};", ""]
    return "\n".join(lines)


def spec_js(flow_name: str, scenarios: list[dict], data_file: str) -> str:
    """One spec per module. Specs call page-object methods, never locators."""
    cls = pascal(flow_name, "Module") + "Page"
    variable = camel(flow_name, "module") + "Page"
    longest = max((len(s.get("steps") or []) for s in scenarios or []), default=0)
    timeout = 90_000 if longest > 8 else 60_000

    lines = [
        f"// {flow_name} — generated from the recorded flow and its scenarios.",
        "const { test, expect } = require('@playwright/test');",
        f"const {{ {cls} }} = require('../pages/{cls}');",
        "const credentials = require('../testdata/credentials.json');",
        f"const data = require('../testdata/{data_file}');",
        "",
        f"test.describe({js_string(flow_name)}, () => {{",
        f"{INDENT}// A real multi-screen flow against a real server legitimately",
        f"{INDENT}// exceeds Playwright's 30s default even when every step succeeds.",
        f"{INDENT}test.setTimeout({timeout:_});".replace("_", "_"),
        "",
        f"{INDENT}let {variable};",
        "",
        f"{INDENT}test.beforeEach(async ({{ page }}) => {{",
        f"{INDENT*2}{variable} = new {cls}(page);",
        f"{INDENT*2}await {variable}.loginAndPrepare(credentials);",
        f"{INDENT}}});",
        "",
    ]

    for scenario in scenarios or []:
        name = str(scenario.get("scenario_name") or "scenario")
        method = camel(name, "runScenario")
        expected = str(scenario.get("expected_result") or "").strip()
        key = camel(name, "case")
        lines.append(f"{INDENT}test({js_string(name)}, async () => {{")
        if expected:
            lines.append(f"{INDENT*2}// Expected: {expected}")
        lines += [
            f"{INDENT*2}await {variable}.{method}(data[{js_string(key)}] || {{}});",
            f"{INDENT}}});",
            "",
        ]
    lines += ["});", ""]
    return "\n".join(lines)


def test_data_for(flow_name: str, scenarios: list[dict],
                  fields: list[dict]) -> tuple[dict, list[dict]]:
    """The module's data file, plus a row per entry for the data table."""
    index = {str(f.get("selector") or "").strip(): field_property(f)[0]
             for f in fields or []}
    data: dict[str, dict] = {}
    notes: list[dict] = []

    for scenario in scenarios or []:
        key = camel(scenario.get("scenario_name") or "case", "case")
        entry: dict[str, Any] = {}
        volatile = False
        for step in scenario.get("steps") or []:
            if str(step.get("action") or "").upper() not in ("TYPE", "SELECT"):
                continue
            prop = index.get(str(step.get("selector") or "").strip())
            if not prop:
                continue
            value = str(step.get("value", ""))
            # A value the platform substitutes at run time is, by definition, one
            # that must be refreshed for a standalone run.
            if "{{" in value:
                volatile = True
                value = ""
            entry[prop] = value
        if entry:
            data[key] = entry
            notes.append({
                "scenario": scenario.get("scenario_name", ""),
                "key": key,
                "fields": len(entry),
                "changes_every_run": volatile,
                "why": ("contains a value the platform generated at run time "
                        "(a captured id or a unique token)" if volatile
                        else "static form data"),
            })
    return data, notes


def playwright_config_js(base_url: str, workers: int = 1) -> str:
    return "\n".join([
        "// @ts-check",
        "const { defineConfig } = require('@playwright/test');",
        "",
        "module.exports = defineConfig({",
        f"{INDENT}testDir: './tests',",
        f"{INDENT}// Real end-to-end flows against a live server are slow by nature.",
        f"{INDENT}timeout: 90_000,",
        f"{INDENT}expect: {{ timeout: 15_000 }},",
        f"{INDENT}// Chained business flows must not race each other.",
        f"{INDENT}workers: {workers},",
        f"{INDENT}fullyParallel: false,",
        f"{INDENT}reporter: [['list'], ['html', {{ open: 'never' }}]],",
        f"{INDENT}use: {{",
        f"{INDENT*2}baseURL: process.env.BASE_URL || {js_string(base_url)},",
        f"{INDENT*2}headless: true,",
        f"{INDENT*2}screenshot: 'only-on-failure',",
        f"{INDENT*2}video: 'retain-on-failure',",
        f"{INDENT*2}trace: 'retain-on-failure',",
        f"{INDENT}}},",
        "});",
        "",
    ])


def package_json(project_name: str) -> str:
    return json.dumps({
        "name": kebab(project_name, "qa-suite"),
        "version": "1.0.0",
        "private": True,
        "description": f"Playwright POM suite exported from the QA platform "
                       f"({project_name}).",
        "scripts": {
            "test": "playwright test",
            "test:headed": "playwright test --headed",
            "report": "playwright show-report",
        },
        "devDependencies": {"@playwright/test": "^1.47.0"},
    }, indent=2) + "\n"


def readme_md(project_name: str, modules: list[dict], data_notes: list[dict],
              gaps: list[str]) -> str:
    lines = [
        f"# {project_name} — Playwright POM suite",
        "",
        "Exported from the QA platform. Deterministically generated from the",
        "recorded flows and their scenarios — no AI wrote this code.",
        "",
        "## Structure",
        "",
        "```",
        "pages/        one class per module; locators live here",
        "tests/        one spec per module; specs call page-object methods only",
        "testdata/     JSON the specs read",
        "playwright.config.js",
        "```",
        "",
        "## Running it",
        "",
        "```bash",
        "npm install",
        "npx playwright install chromium",
        "cp .env.example .env      # then fill in BASE_URL and credentials",
        "npx playwright test",
        "```",
        "",
        "## Modules",
        "",
        "| Module | Page object | Spec | Scenarios |",
        "| --- | --- | --- | --- |",
    ]
    for module in modules:
        lines.append(f"| {module['flow']} | `pages/{module['page']}` | "
                     f"`tests/{module['spec']}` | {module['scenarios']} |")

    lines += ["", "## Test data", "",
              "| Entry | Module | Changes every run? | Why |",
              "| --- | --- | --- | --- |"]
    for note in data_notes:
        mark = "**Yes**" if note["changes_every_run"] else "No"
        lines.append(f"| `{note['key']}` | {note['module']} | {mark} | {note['why']} |")

    if gaps:
        lines += ["", "## Known gaps", "",
                  "Steps the platform verified internally that have no direct",
                  "Playwright equivalent. Each is marked with a `TODO` in the code",
                  "rather than silently checking nothing:", ""]
        for gap in gaps:
            lines.append(f"- {gap}")

    lines += [
        "", "## House rules encoded in the generated code", "",
        "- A control with more than one recorded selector becomes `a.or(b)`, so a",
        "  conditionally rendered field or a duplicate label does not break the run.",
        "- Nothing is counted before it is waited for — `.count()` does not retry",
        "  the way `expect()` does.",
        "- A click that removes its own element is bounded and then asserted on the",
        "  resulting state, never on the click settling.",
        "- Long flows carry an explicit `test.setTimeout()`.",
        "- Form fills are guarded by a visibility check.",
        "",
    ]
    return "\n".join(lines)


def env_example(base_url: str) -> str:
    return "\n".join([
        "# Copy to .env and fill in. Never commit real credentials.",
        f"BASE_URL={base_url}",
        "QA_USERNAME=",
        "QA_PASSWORD=",
        "",
    ])


# ──────────────────────────────────────────────────────────────────────
# Assembly
# ──────────────────────────────────────────────────────────────────────
def build_project(project_name: str, modules: list[dict],
                  base_url: str = "") -> dict[str, str]:
    """Every file of the exported project, as ``{path: contents}``.

    ``modules`` is a list of ``{flow, fields, actions, scenarios}``. Pure: it
    reads nothing and writes nothing, which is what keeps this feature entirely
    disconnected from the platform's own data.
    """
    files: dict[str, str] = {}
    manifest: list[dict] = []
    data_notes: list[dict] = []
    gaps: set[str] = set()

    login = next((m for m in modules
                  if "login" in str(m.get("flow", "")).lower()), None)
    login_steps = (login or {}).get("actions") or []
    resolved_base = base_url or (login or (modules[0] if modules else {})).get(
        "target_url", "")

    files["pages/BasePage.js"] = base_page_js(login_steps, resolved_base)

    for module in modules:
        flow = str(module.get("flow") or "Module")
        fields = module.get("fields") or []
        actions = module.get("actions") or []
        scenarios = module.get("scenarios") or []
        if not scenarios:
            continue

        page_name = pascal(flow, "Module") + "Page.js"
        spec_name = kebab(flow, "module") + ".spec.js"
        data_name = kebab(flow, "module") + ".json"

        for scenario in scenarios:
            for step in scenario.get("steps") or []:
                action = str(step.get("action") or "").upper()
                if action in ENGINE_ONLY_ACTIONS:
                    gaps.add(action)

        files[f"pages/{page_name}"] = page_object_js(flow, fields, actions, scenarios)
        files[f"tests/{spec_name}"] = spec_js(flow, scenarios, data_name)

        data, notes = test_data_for(flow, scenarios, fields)
        files[f"testdata/{data_name}"] = json.dumps(data, indent=2) + "\n"
        for note in notes:
            note["module"] = flow
            data_notes.append(note)

        manifest.append({"flow": flow, "page": page_name, "spec": spec_name,
                         "scenarios": len(scenarios)})

    files["testdata/credentials.json"] = json.dumps(
        {"username": "", "password": ""}, indent=2) + "\n"
    files["playwright.config.js"] = playwright_config_js(resolved_base)
    files["package.json"] = package_json(project_name)
    files[".env.example"] = env_example(resolved_base)
    files[".gitignore"] = "node_modules/\ntest-results/\nplaywright-report/\n.env\n"
    files["README.md"] = readme_md(project_name, manifest, data_notes, sorted(gaps))
    return files
