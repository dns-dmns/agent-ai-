"""How the verdict engine reads an expected result, pinned phrase by phrase.

A misread expectation is the most damaging bug this platform can have: reading a
positive test as a negative one turns a correct save into a FAIL that blames a
working application. Every phrase here is a wording real suites use. Add to the
table whenever a misclassification is found -- never loosen an existing row.
"""
import pytest

from app.verdict import classify_expectation


def _reading(text: str, declared: str = "") -> str:
    intent = classify_expectation(text, declared)
    if intent["expects_error"]:
        return "error"
    if intent["expects_success"]:
        return "success"
    for key in ("absence", "persistence", "sanitised", "stability"):
        if intent[f"expects_{key}"]:
            return key
    return "none"


POSITIVE = [
    # error vocabulary used as a precondition, not an outcome
    "With all required fields filled, the record is saved successfully",
    "Mandatory fields are filled and the customer is created successfully",
    "After entering all mandatory details the form is submitted",
    "Enter valid data in every required field; the item is saved",
    "Duplicate check passes and the item is created",
    # negated error vocabulary
    "No error is shown and the record is saved",
    "Record saves without any validation error",
    "The form submits without errors",
    "Save should not fail; success toast appears",
    "Saved successfully, no validation messages",
    # plain positives
    "Customer is created and appears in the grid",
    "Success message 'Saved successfully' is displayed",
    "User is redirected to the dashboard",
]

NEGATIVE = [
    "A validation error 'Name is required' is displayed",
    "The record is not saved and an error appears",
    "System rejects the duplicate code",
    "Error message shown: Invalid email",
    "Saving fails with 'Code already exists'",
    "The user cannot save without a name",
    "Customer Name is required",
    "Save is blocked and the field is highlighted",
    "An error appears and the record is not created",
    "Validation error is shown even though other fields are saved",
    "Duplicate entry is not allowed",
    "Login is denied for a locked account",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_positive_expectations_are_not_read_as_errors(text):
    assert _reading(text) == "success", text


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_expectations_are_read_as_errors(text):
    assert _reading(text) == "error", text


@pytest.mark.parametrize("declared,expected", [
    ("error", "error"), ("success", "success"), ("absence", "absence"),
    ("persistence", "persistence"), ("sanitised", "sanitised"), ("stability", "stability"),
])
def test_a_declared_intent_wins_over_the_wording(declared, expected):
    # Wording that says the opposite must not matter once the intent is declared.
    assert _reading("record is saved successfully with no error", declared) == expected


def test_an_unknown_declared_intent_falls_back_to_the_wording():
    assert _reading("The record is saved successfully", "bogus") == "success"


# ── A sheet row's Type declares the intent unless the wording contradicts it ──
from app import user_cases  # noqa: E402


@pytest.mark.parametrize("case_type,expected,intent,warns", [
    ("Positive", "Data has been stored", "success", False),     # no success vocabulary
    ("negative", "Customer Name is required", "error", False),
    ("Negative", "Code already exists", "error", False),
    ("positive", "Customer Name is required", "", True),        # contradiction: reported
    ("negative", "The record is saved successfully", "", True),
    ("boundary", "The record saves", "", False),                # either way: undeclared
    ("High", "Saved", "", False),                               # a Priority column
])
def test_row_type_declares_intent(case_type, expected, intent, warns):
    got, warning = user_cases.intent_from_type(case_type, expected)
    assert got == intent
    assert bool(warning) is warns
