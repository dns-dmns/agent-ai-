"""Application explorer: the pure parts (names, samples, selector clean-up)."""
import os
import tempfile

os.environ["QA_WORKSPACE"] = os.environ.get("QA_WORKSPACE") or tempfile.mkdtemp(prefix="qa_explore_")

from app import explorer  # noqa: E402


def test_menu_path_becomes_a_folder_path():
    assert explorer.flow_name_for(["Master", "Product Catalogue", "Item Master"]) == \
        "Master/Product Catalogue/Item Master"
    # characters a folder cannot hold are replaced, never passed through
    assert explorer.flow_name_for(["Sales / Returns", "A:B*C"]) == "Sales - Returns/A-B-C"
    assert explorer.flow_name_for(["Stock Radar (MRP)"]) == "Stock Radar (MRP)"


def test_sensitive_and_unfillable_fields_are_never_typed():
    for f in ({"kind": "password", "label": "Password *"},
              {"kind": "text", "label": "OTP"},
              {"kind": "text", "label": "Captcha"},
              {"kind": "file", "label": "Upload"},
              {"kind": "date", "label": "Order Date"},
              {"kind": "toggle", "label": "Active"}):
        assert explorer.sample_value(f) is None, f


def test_sample_values_fit_the_field():
    assert explorer.sample_value({"kind": "email", "label": "Email *"}) == "qa.auto@example.com"
    assert explorer.sample_value({"kind": "text", "label": "Phone *"}) == "9876543210"
    assert explorer.sample_value({"kind": "number", "label": "Unit Price"}) == "10"
    assert explorer.sample_value({"kind": "text", "label": "GSTIN"}) == "29ABCDE1234F1Z5"
    assert explorer.sample_value({"kind": "text", "label": "IFSC / RTGS *"}) == "SBIN0001234"
    assert explorer.sample_value({"kind": "text", "label": "Depot Code *"}) == "QAAUTO01"
    assert explorer.sample_value({"kind": "text", "label": "Customer Name *"}).startswith("QA-AUTO")


def test_field_name_replaces_a_shared_placeholder():
    actions = [{"action": "CLICK", "selector": 'input[placeholder="0.00"]', "selectors": ['input[placeholder="0.00"]']},
               {"action": "TYPE", "selector": 'input[placeholder="0.00"]', "selectors": ['input[placeholder="0.00"]']},
               {"action": "CLICK", "selector": '[aria-label="Domestic"]', "selectors": []}]
    precise = explorer.Explorer.precise_selector({"name": "unitPrice", "kind": "number"})
    explorer.Explorer.pin_selector(actions, 0, precise)
    assert actions[1]["selector"] == '[formcontrolname="unitPrice"] input'
    assert actions[1]["selectors"][1] == 'input[placeholder="0.00"]'      # kept as fallback
    assert actions[2]["selector"] == '[aria-label="Domestic"]'           # options untouched


def test_tidy_drops_commit_tab_and_generated_ids():
    actions = [{"action": "KEYPRESS", "selector": "#a", "value": "Tab"},
               {"action": "TYPE", "selector": "#a", "value": "x"},
               {"action": "VERIFY_TEXT", "selector": "#pn_id_10_header",
                "selectors": ["#pn_id_10_header", ".p-dialog-title"]},
               {"action": "KEYPRESS", "selector": "#b", "value": "Enter"}]
    out = explorer.tidy(actions)
    assert [a["action"] for a in out] == ["TYPE", "VERIFY_TEXT", "KEYPRESS"]
    assert out[1]["selector"] == ".p-dialog-title"


def test_session_attaches_effects_and_carries_required_forward():
    s = explorer.Session()
    s.handle({"action": "CLICK", "selector": "#x"})          # ignored before start
    s.start()
    s.handle({"action": "CLICK", "selector": "#save", "required": True})
    s.handle({"action": "__EFFECT__", "selector": "#save", "effect": {"messages": [{"text": "Saved"}]}})
    s.handle({"action": "TYPE", "selector": "#save", "required": False})
    acts = s.stop()
    assert len(acts) == 2
    assert acts[0]["effect"]["messages"][0]["text"] == "Saved"
    assert acts[1]["required"] is True


def test_inserted_steps_borrow_the_nearest_screenshot():
    actions = [{"action": "CLICK", "screenshot": None},          # inserted: open dropdown
               {"action": "CLICK", "screenshot": "/s/step_1.jpg"},
               {"action": "VERIFY_DISABLED", "screenshot": None}]  # inserted at the end
    out = explorer.fill_screenshots(actions)
    assert [a["screenshot"] for a in out] == ["/s/step_1.jpg"] * 3
