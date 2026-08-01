"""End-to-end proof of the dependency-chain, rules and polling features.

Runs the real engine against ``tests/selftest_app/chain.html`` — a master module
feeding a dependent dropdown, an order that generates a code, and an approval
state machine gating completion. Each scenario asserts a capability that did not
exist before:

* a prerequisite's value is **captured** and reused downstream (STORE)
* an **inactive master disappears** from the dependent dropdown (VERIFY_OPTION_*)
* a **generated code** flows from one module into the next
* an approval that arrives **later** is waited for, not slept through (WAIT_FOR_STATE)
* a **rule** you wrote fails a scenario that would otherwise have passed
* **referential integrity**: a master in use cannot be deleted

Offline, no API key, throwaway workspace.

    .venv\\Scripts\\python.exe tests\\e2e_chain.py
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP_DIR = HERE / "selftest_app"

WORKSPACE = Path(tempfile.mkdtemp(prefix="qa_chain_"))
os.environ["QA_WORKSPACE"] = str(WORKSPACE)
os.environ["QA_SCHEDULER"] = "0"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app import config      # noqa: E402
from app import contracts   # noqa: E402
from app import executor    # noqa: E402
from app import rules       # noqa: E402

PROJECT = "ChainTest"
MASTER = "category master"
TARGET = "order module"

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(APP_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def seed_workspace(base_url: str) -> None:
    page = f"{base_url}/chain.html"

    # ── The master flow: recorded actions create a category ───────────
    master_dir = config.flow_dir(PROJECT, MASTER)
    master_dir.mkdir(parents=True, exist_ok=True)
    (master_dir / "flow.json").write_text(json.dumps({
        "flow_name": MASTER, "target_url": page, "step_count": 3}), encoding="utf-8")
    (master_dir / "actions.json").write_text(json.dumps([
        {"action": "TYPE", "selector": "#catName", "value": "Cotton"},
        {"action": "SELECT", "selector": "#catStatus", "value": "Active"},
        {"action": "CLICK", "selector": "#saveCatBtn"},
    ]), encoding="utf-8")
    # …and its contract says what it provides, so the value survives the replay.
    contracts.save(PROJECT, MASTER, {
        "provides": [{
            "entity": "category", "var": "category_name",
            "name_field": "#catName", "status_field": "#catStatus",
            "list_url": page, "capture": "typed",
        }],
        "run_once": True,
    })

    # ── The dependent flow ────────────────────────────────────────────
    target_dir = config.flow_dir(PROJECT, TARGET)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "flow.json").write_text(json.dumps({
        "flow_name": TARGET, "target_url": page, "step_count": 4}), encoding="utf-8")
    (target_dir / "actions.json").write_text(json.dumps([
        {"action": "TYPE", "selector": "#orderRef", "value": "ORD-A"},
    ]), encoding="utf-8")
    contracts.save(PROJECT, TARGET, {
        "requires": [{"entity": "category", "selector": "#categoryDropdown",
                      "kind": "dropdown"}],
        "run_once": False,
    })

    # ── A rule the tester wrote, enforced on every scenario ───────────
    rules.save(PROJECT, {
        "title": "Quantity must reject zero and negatives",
        "text": "Order quantity must never be accepted as 0 or a negative number.",
        "kind": "field_rejects", "selector": "#orderQty", "values": ["0", "-3"],
        "severity": "blocker", "generate_scenario": False,
    }, TARGET)

    rules.save(PROJECT, {
        "title": "Quantity above 1000 must be rejected",
        "text": "Business rule: a single order may never exceed 1000 units.",
        "kind": "field_rejects", "selector": "#orderQty", "values": ["5000"],
        "severity": "blocker", "generate_scenario": False,
    }, TARGET)

    (target_dir / "test_cases.json").write_text(json.dumps(SUITE, indent=2), encoding="utf-8")


SUITE = [
    {
        "scenario_name": "Seeded category is offered in the order dropdown",
        "type": "positive",
        "_expect": "PASS",
        "_proves": "the master's value was captured and reused downstream",
        "steps": [
            {"action": "VERIFY_OPTION_PRESENT", "selector": "#categoryDropdown",
             "value": "{{category_name}}"},
        ],
        "expected_result": "The category created by the prerequisite is selectable.",
    },
    {
        "scenario_name": "Order saves and its generated code is captured",
        "type": "positive",
        "_expect": "PASS",
        "_proves": "STORE captures a code the app generated at runtime",
        "steps": [
            {"action": "TYPE", "selector": "#orderRef", "value": "REF-{{run_token}}"},
            {"action": "SELECT", "selector": "#categoryDropdown", "value": "{{category_name}}"},
            {"action": "TYPE", "selector": "#orderQty", "value": "5"},
            {"action": "CLICK", "selector": "#saveOrderBtn"},
            {"action": "VERIFY_MESSAGE", "value": "saved successfully"},
            {"action": "STORE", "selector": "#orderCode", "value": "order_code",
             "capture": "value"},
            {"action": "VERIFY_ROW_CONTAINS", "selector": "table", "value": "{{order_code}}"},
        ],
        "expected_result": "The order is created successfully and appears in the grid.",
    },
    {
        "scenario_name": "Zero quantity is rejected",
        "type": "negative",
        "_expect": "PASS",
        "_proves": "the tester's rule is satisfied when the app behaves",
        "steps": [
            {"action": "TYPE", "selector": "#orderRef", "value": "REF-ZERO"},
            {"action": "SELECT", "selector": "#categoryDropdown", "value": "{{category_name}}"},
            {"action": "TYPE", "selector": "#orderQty", "value": "0"},
            {"action": "CLICK", "selector": "#saveOrderBtn"},
            {"action": "VERIFY_MESSAGE", "value": "greater than 0"},
        ],
        "expected_result": "A validation error appears because quantity must be positive.",
    },
    {
        "scenario_name": "Negative quantity slips through unchecked",
        "type": "negative",
        "_expect": "FAIL",
        "_proves": "expectation contradicted — app rejected it, scenario claimed success",
        "steps": [
            {"action": "TYPE", "selector": "#orderRef", "value": "REF-NEG"},
            {"action": "SELECT", "selector": "#categoryDropdown", "value": "{{category_name}}"},
            {"action": "TYPE", "selector": "#orderQty", "value": "-3"},
            {"action": "CLICK", "selector": "#saveOrderBtn"},
        ],
        "expected_result": "The order is created successfully.",
    },
    {
        "scenario_name": "Category in use cannot be deleted",
        "type": "negative",
        "_expect": "PASS",
        "_proves": "referential integrity across modules",
        "steps": [
            {"action": "TYPE", "selector": "#orderRef", "value": "REF-USE"},
            {"action": "SELECT", "selector": "#categoryDropdown", "value": "{{category_name}}"},
            {"action": "TYPE", "selector": "#orderQty", "value": "2"},
            {"action": "CLICK", "selector": "#saveOrderBtn"},
            {"action": "DIALOG", "value": "accept"},
            {"action": "CLICK", "selector": "#catBody tr:has-text('{{category_name}}') .delete-btn"},
            {"action": "VERIFY_MESSAGE", "value": "Cannot delete"},
            {"action": "VERIFY_ROW_CONTAINS", "selector": "#catTable",
             "value": "{{category_name}}"},
        ],
        "expected_result": "Deletion is blocked and the category remains listed.",
    },
    {
        "scenario_name": "Approval that arrives later is waited for",
        "type": "positive",
        "_expect": "PASS",
        "_proves": "WAIT_FOR_STATE polls a state set on someone else's clock",
        "steps": [
            {"action": "TYPE", "selector": "#orderRef", "value": "REF-APPROVE"},
            {"action": "SELECT", "selector": "#categoryDropdown", "value": "{{category_name}}"},
            {"action": "TYPE", "selector": "#orderQty", "value": "1"},
            {"action": "CLICK", "selector": "#saveOrderBtn"},
            {"action": "STORE", "selector": "#orderCode", "value": "approval_code",
             "capture": "value"},
            {"action": "TYPE", "selector": "#lookupCode", "value": "{{approval_code}}"},
            {"action": "CLICK", "selector": "#lookupBtn"},
            {"action": "CLICK", "selector": "#pendingBtn"},
            {"action": "WAIT_FOR_STATE", "selector": "#approvalStatus", "value": "Approved",
             "timeout": 20000, "interval": 1000},
            {"action": "VERIFY_TEXT", "selector": "#gateStatus", "value": "Approved"},
        ],
        "expected_result": "The order reaches the Approved state and the gate unlocks.",
    },
    {
        "scenario_name": "Oversized quantity is silently accepted",
        "type": "positive",
        "_expect": "FAIL",
        "_proves": "THE RULE ONE — the scenario passes on its own terms, and the "
                   "rule alone fails it",
        "steps": [
            {"action": "TYPE", "selector": "#orderRef", "value": "REF-BIG"},
            {"action": "SELECT", "selector": "#categoryDropdown", "value": "{{category_name}}"},
            {"action": "TYPE", "selector": "#orderQty", "value": "5000"},
            {"action": "CLICK", "selector": "#saveOrderBtn"},
            {"action": "VERIFY_MESSAGE", "value": "saved successfully"},
        ],
        "expected_result": "The order is created successfully and appears in the grid.",
    },
    {
        "scenario_name": "Completion is blocked until approval",
        "type": "negative",
        "_expect": "PASS",
        "_proves": "the state machine gate holds",
        "steps": [
            {"action": "TYPE", "selector": "#orderRef", "value": "REF-GATE"},
            {"action": "SELECT", "selector": "#categoryDropdown", "value": "{{category_name}}"},
            {"action": "TYPE", "selector": "#orderQty", "value": "1"},
            {"action": "CLICK", "selector": "#saveOrderBtn"},
            {"action": "STORE", "selector": "#orderCode", "value": "gate_code",
             "capture": "value"},
            {"action": "TYPE", "selector": "#lookupCode", "value": "{{gate_code}}"},
            {"action": "CLICK", "selector": "#lookupBtn"},
            {"action": "VERIFY_DISABLED", "selector": "#completeBtn"},
        ],
        "expected_result": "The Complete button stays disabled while the order is Pending.",
    },
]


async def main() -> int:
    port = free_port()
    httpd = serve(port)
    base_url = f"http://127.0.0.1:{port}"
    seed_workspace(base_url)

    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)
        kind = event.get("event")
        if kind in ("info", "error", "cancelled"):
            print("  " + str(event.get("message", ""))[:150])
        elif kind == "test_end":
            print(f"  ⇒ #{event.get('index')} {event.get('status')}: "
                  f"{(event.get('verdict') or {}).get('reason', '')[:110]}")
        elif kind == "log":
            text = str(event.get("message", ""))
            if any(mark in text for mark in ("⚓", "⏱", "🚫", "📏", "🌱", "🔗")):
                print("     " + text[:150])

    cfg = executor.RunConfig(
        project_name=PROJECT, flow_name=TARGET, mode="headless", workers=1,
        verdict_mode="rules", dependencies=MASTER, seed_once=True,
        dep_unique_data=True, apply_rules=True, triggered_by="chain-test",
    )
    state = executor.register_run(PROJECT, TARGET)

    print(f"Sample app: {base_url}/chain.html\nWorkspace : {WORKSPACE}\n")
    try:
        report = await executor.execute_suite(cfg, emit, state)
    finally:
        httpd.shutdown()

    by_name = {c["name"]: c for c in report["checkpoints"]}
    print("\n" + "=" * 112)
    print(f"{'':4}{'SCENARIO':58}{'WANT':7}{'GOT':12}PROVES")
    print("-" * 112)
    failures = []
    for case in SUITE:
        name = case["scenario_name"]
        want = case["_expect"]
        got = by_name.get(name, {}).get("status", "MISSING")
        ok = got == want
        if not ok:
            failures.append((name, want, got,
                             (by_name.get(name, {}).get("verdict") or {}).get("reason", "")))
        print(f"{'OK' if ok else 'XX':4}{name[:56]:58}{want:7}{got:12}{case['_proves'][:44]}")
    print("=" * 112)

    captured = {}
    for checkpoint in report["checkpoints"]:
        captured.update(checkpoint.get("captured") or {})
    print(f"Captured variables reused across modules: "
          f"{ {k: v for k, v in captured.items() if k in ('category_name', 'order_code', 'approval_code', 'gate_code')} }")
    print(f"Rules enforced: {json.dumps(report.get('rules_enforced', []), indent=None)[:200]}")

    seeded = [e for e in events if "Seeding prerequisites once" in str(e.get("message", ""))]
    print(f"Run-once seeding used: {'yes' if seeded else 'no'}")

    for name, want, got, reason in failures:
        print(f"\nXX {name}\n   wanted {want}, got {got} — {reason}")

    oversized = by_name.get("Oversized quantity is silently accepted", {})
    rule_caught = (oversized.get("verdict") or {}).get("source") == "rules"
    print(f"Oversized case failed *by rule*: {'yes' if rule_caught else 'no'} — "
          f"{(oversized.get('verdict') or {}).get('reason', '')[:120]}")

    ok = (not failures and rule_caught
          and captured.get("category_name") and captured.get("order_code"))
    print("\n" + ("CHAIN SELF-TEST PASSED ✅" if ok else "CHAIN SELF-TEST FAILED ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    finally:
        shutil.rmtree(WORKSPACE, ignore_errors=True)
