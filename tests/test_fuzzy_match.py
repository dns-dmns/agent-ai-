"""Unit tests for typo-tolerant flow matching in app.defects."""

from app import defects


def test_match_flow_typo_tolerance():
    flows = ["Paper Core", "Beam Length", "Circular Machine", "Fabric Category"]

    # Exact matches
    assert defects.match_flow("Paper Core", flows) == "Paper Core"
    assert defects.match_flow("Master Data -> Paper Core", flows) == "Paper Core"

    # Typo tolerance
    assert defects.match_flow("Paper Corre", flows) == "Paper Core"
    assert defects.match_flow("Fabric Catgory", flows) == "Fabric Category"
    assert defects.match_flow("Masters / Beem Length", flows) == "Beam Length"

    # Unrelated modules safely rejected
    assert defects.match_flow("Customer Master", flows) == ""
    assert defects.match_flow("Payment Gateway", flows) == ""
    assert defects.match_flow("", flows) == ""
