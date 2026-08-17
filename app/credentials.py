"""Keep recorded credentials out of the files that reach git.

A recording types a real username and password, and the recorder used to save
both as plain literals in ``actions.json`` — which is committed, and from which
generation copied them into ``spec.json`` and ``test_cases.json``. Three files
in version control carrying a working login for the system under test.

The fix is substitution at save time. A TYPE step on a credential field is
stored as ``{{username}}`` / ``{{password}}``, and the literal goes to the
project's ``login_data.json`` — a name the repo's ``.gitignore`` already
excludes at any depth. At run time the engine resolves the placeholder from the
project's credentials, so the flow replays exactly as recorded; the secret just
never sits in a tracked file.

Only the username/password pair is substituted. OTP and captcha values are
useless after the session that produced them, and an expired literal is more
honest than a placeholder nothing can fill.
"""

from __future__ import annotations

import json

from . import config

log = config.get_logger("credentials")

#: Substituted markers. Deliberately narrower than the grounding classifier's
#: CREDENTIAL_FIELD_MARKERS: "mobile"/"otp"/"captcha" mark a field as sensitive,
#: but only the username/password pair has a stable value worth resolving later.
USERNAME_MARKERS = ("user", "login", "email")
PASSWORD_MARKERS = ("pass", "pwd")

USERNAME_PLACEHOLDER = "{{username}}"
PASSWORD_PLACEHOLDER = "{{password}}"


def _blob(action: dict) -> str:
    return (f"{action.get('selector', '')} {action.get('label', '')} "
            f"{action.get('text', '')}").lower()


def classify_field(action: dict) -> str:
    """``"username"``, ``"password"`` or ``""`` for a recorded action's target.

    Password wins when both match ("user_password" contains "user" too) —
    typing a username into a password field is the worse mistake.
    """
    blob = _blob(action)
    if any(marker in blob for marker in PASSWORD_MARKERS):
        return "password"
    if any(marker in blob for marker in USERNAME_MARKERS):
        return "username"
    return ""


def store_file(project_name: str):
    return config.project_dir(project_name) / "login_data.json"


def load_stored(project_name: str) -> dict:
    path = store_file(project_name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as err:  # noqa: BLE001 - a corrupt store must not stop a run
        log.warning("Could not read %s: %s", path, err)
        return {}


def store(project_name: str, **values: str) -> None:
    """Merge non-empty values into the project's gitignored credential store."""
    fresh = {k: v for k, v in values.items() if v}
    if not fresh:
        return
    current = load_stored(project_name)
    current.update(fresh)
    path = store_file(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")


def scrub_recorded_actions(actions: list, project_name: str) -> dict:
    """Replace credential literals in TYPE actions with placeholders, in place.

    The literals are persisted via :func:`store` so the run can resolve the
    placeholders again. Values that are already placeholders are left alone, and
    a wrong-password literal in a negative scenario never reaches this function —
    it only sees what the recorder captured, which is the real login.

    Returns ``{"username": ..., "password": ...}`` for whatever was found.
    """
    found: dict = {}
    stored = load_stored(project_name)
    for action in actions or []:
        if (action.get("action") or "").upper() != "TYPE":
            continue
        value = str(action.get("value") or "")
        if not value or value.startswith("{{"):
            # Already scrubbed — but the `text` echo may still hold the literal
            # from a pass made before the echo was cleaned. Finish the job.
            text = str(action.get("text") or "")
            if text and text in (stored.get("username"), stored.get("password")):
                action["text"] = ""
            continue
        kind = classify_field(action)
        if kind == "username":
            found["username"] = value
            action["value"] = USERNAME_PLACEHOLDER
        elif kind == "password":
            found["password"] = value
            action["value"] = PASSWORD_PLACEHOLDER
        # The recorder also echoes what was typed into `text` — scrubbing the
        # value while leaving the echo would move the literal one key over.
        if kind and str(action.get("text") or "") == value:
            action["text"] = ""
    # Effect blocks capture what the app *displayed* after an action — which,
    # after a login, includes the username on screen. Masked with the same
    # placeholder: an assertion generated from it then follows the credential
    # instead of breaking the day the account changes. Exact whole-text match
    # only, so "Login Successful" and other wording is untouched.
    literals = dict(stored)
    literals.update(found)
    swap = {literals.get("username"): USERNAME_PLACEHOLDER,
            literals.get("password"): PASSWORD_PLACEHOLDER}
    swap.pop(None, None)
    if swap:
        for action in actions or []:
            for message in ((action.get("effect") or {}).get("messages") or []):
                if isinstance(message, dict) and message.get("text") in swap:
                    message["text"] = swap[message["text"]]

    if found:
        store(project_name, **found)
        log.info("Recorded credentials for %s moved to %s (%s substituted).",
                 project_name, store_file(project_name).name,
                 ", ".join(sorted(found)))
    return found


def scrub_known_literals(node, literals: dict) -> int:
    """Replace exact credential literals in generated suites, nothing else.

    Generated test cases deliberately type wrong values into the same fields —
    ``wrong1``, ``' OR '1'='1``, three spaces — and each of those is the point
    of its scenario. Substituting every credential-field value (what
    :func:`scrub_recorded_actions` correctly does to a raw recording of a real
    login) would turn "invalid password is rejected" into typing the *valid*
    password. So here a value is replaced only when it is byte-for-byte the real
    credential, and only on a field classified as that credential's kind.

    Walks any JSON structure in place; returns how many values were replaced.
    """
    placeholders = {"username": USERNAME_PLACEHOLDER, "password": PASSWORD_PLACEHOLDER}
    replaced = 0

    def walk(item) -> None:
        nonlocal replaced
        if isinstance(item, dict):
            if (item.get("action") or "").upper() == "TYPE":
                kind = classify_field(item)
                literal = literals.get(kind)
                if kind and literal and str(item.get("value") or "") == str(literal):
                    item["value"] = placeholders[kind]
                    replaced += 1
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for element in item:
                walk(element)

    walk(node)
    return replaced


def scrub_data_rows(node, literals: dict) -> int:
    """Replace exact credential literals in data tables, in place.

    Data files carry credentials as plain key/value rows —
    ``{"username": "admin", "password": "1234"}`` — not as TYPE steps, so the
    step-based scrubs never see them. Same exactness rule: a row holding a
    deliberately wrong password keeps it.

    At run time the executor skips a self-referential row value
    (``username: "{{username}}"``) when seeding the pool, so the placeholder
    resolves to the real credential instead of to itself.
    """
    placeholders = {"username": USERNAME_PLACEHOLDER, "password": PASSWORD_PLACEHOLDER}
    replaced = 0

    def walk(item) -> None:
        nonlocal replaced
        if isinstance(item, dict):
            for key, value in item.items():
                low = str(key).lower()
                if low in placeholders and literals.get(low) \
                        and str(value) == str(literals[low]):
                    item[key] = placeholders[low]
                    replaced += 1
                else:
                    walk(value)
        elif isinstance(item, list):
            for element in item:
                walk(element)

    walk(node)
    return replaced
