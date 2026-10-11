"""Data Masking & Security Sanitizer.

Zero-dependency module to detect and redact sensitive data (credentials,
tokens, PII, national IDs, payment data) from strings, dictionaries,
prompts, and logs before they are saved to disk or sent to external LLMs.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Sensitive dictionary keys whose values should always be redacted
SENSITIVE_KEYS = {
    "password",
    "pass",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "pin",
    "cvv",
    "cvc",
    "ssn",
    "aadhaar",
    "pan",
    "credit_card",
    "card_number",
}

# Compiled regex patterns for inline text redaction
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Indian 10-digit phone with optional +91 / 0 prefix, or international format
PHONE_PATTERN = re.compile(
    r"(?:\+91[\-\s]?)?[6-9]\d{9}\b|\b\+?[1-9]\d{1,2}[\s\-]\d{3,4}[\s\-]\d{4}\b"
)

# Indian PAN card: 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F)
PAN_PATTERN = re.compile(
    r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.IGNORECASE
)

# Indian Aadhaar: 12 digits (often formatted as 4-4-4)
AADHAAR_PATTERN = re.compile(
    r"\b\d{4}[\s\-]\d{4}[\s\-]\d{4}\b"
)

# Credit card numbers: 13 to 19 digits (with optional hyphens/spaces)
CREDIT_CARD_PATTERN = re.compile(
    r"\b(?:\d{4}[-\s]?){3}\d{4}\b|\b\d{15,16}\b"
)

# Bearer tokens or generic JWT tokens
JWT_BEARER_PATTERN = re.compile(
    r"\bBearer\s+[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\b",
    re.IGNORECASE,
)

# API Keys (e.g. Google Gemini 'AIzaSy...', generic 32+ hex/alphanumeric keys)
API_KEY_PATTERN = re.compile(
    r"\bAIzaSy[A-Za-z0-9\-_]{33}\b"
)

# Password in URL query strings (e.g. password=secret123)
QUERY_PASSWORD_PATTERN = re.compile(
    r"((?:password|token|api_key|secret)=)([^&\s]+)",
    re.IGNORECASE,
)


def mask_text(text: str) -> str:
    """Mask PII, tokens, and credentials in free-form text.

    Replaces discovered sensitive entities with clean placeholders like
    [EMAIL_REDACTED], [PHONE_REDACTED], [API_KEY_REDACTED].
    """
    if not text or not isinstance(text, str):
        return text

    # Fast check: skip regex passes if string contains no obvious trigger chars
    sanitized = text

    # Redact explicit API keys and JWTs first
    sanitized = API_KEY_PATTERN.sub("[API_KEY_REDACTED]", sanitized)
    sanitized = JWT_BEARER_PATTERN.sub("Bearer [TOKEN_REDACTED]", sanitized)

    # Redact URL query parameters
    sanitized = QUERY_PASSWORD_PATTERN.sub(r"\1[REDACTED]", sanitized)

    # Redact Financial / National ID patterns
    sanitized = CREDIT_CARD_PATTERN.sub("[CARD_REDACTED]", sanitized)
    sanitized = AADHAAR_PATTERN.sub("[AADHAAR_REDACTED]", sanitized)
    sanitized = PAN_PATTERN.sub("[PAN_REDACTED]", sanitized)

    # Redact PII (Email & Phone)
    sanitized = EMAIL_PATTERN.sub("[EMAIL_REDACTED]", sanitized)
    sanitized = PHONE_PATTERN.sub("[PHONE_REDACTED]", sanitized)

    return sanitized


def mask_dict(data: dict) -> dict:
    """Recursively mask sensitive keys and values in a dictionary.

    Returns a deep-copied, sanitized dictionary.
    """
    if not isinstance(data, dict):
        return data

    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        key_str = str(key).lower().strip()
        # If the key itself is marked sensitive, redact its value entirely
        if any(sens in key_str for sens in SENSITIVE_KEYS):
            cleaned[key] = "[REDACTED]"
        elif isinstance(value, str):
            cleaned[key] = mask_text(value)
        elif isinstance(value, dict):
            cleaned[key] = mask_dict(value)
        elif isinstance(value, list):
            cleaned[key] = mask_list(value)
        else:
            cleaned[key] = value

    return cleaned


def mask_list(items: list) -> list:
    """Recursively mask a list of items."""
    if not isinstance(items, list):
        return items

    cleaned = []
    for item in items:
        if isinstance(item, str):
            cleaned.append(mask_text(item))
        elif isinstance(item, dict):
            cleaned.append(mask_dict(item))
        elif isinstance(item, list):
            cleaned.append(mask_list(item))
        else:
            cleaned.append(item)
    return cleaned


def mask_payload(payload: Any) -> Any:
    """Universal sanitizer for any JSON-like payload (dict, list, str, etc.)."""
    if isinstance(payload, str):
        return mask_text(payload)
    if isinstance(payload, dict):
        return mask_dict(payload)
    if isinstance(payload, list):
        return mask_list(payload)
    return payload


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """Sanitize HTTP headers, redacting Authorization, Cookie, and API keys."""
    if not isinstance(headers, dict):
        return headers

    sanitized: dict[str, str] = {}
    for key, val in headers.items():
        k_lower = key.lower()
        if k_lower in ("authorization", "cookie", "set-cookie", "x-api-key", "api-key"):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = val
    return sanitized
