"""Unit tests for app.mask (Data Masking & Security Sanitizer)."""

from app.mask import (
    mask_text,
    mask_dict,
    mask_headers,
)


def test_mask_email():
    text = "Please reach out to test.user@example.com for support."
    masked = mask_text(text)
    assert "[EMAIL_REDACTED]" in masked
    assert "test.user@example.com" not in masked


def test_mask_phone_numbers():
    text_indian = "Contact: +91 9876543210 or 9876543210 immediately."
    masked = mask_text(text_indian)
    assert "[PHONE_REDACTED]" in masked
    assert "9876543210" not in masked


def test_mask_credentials_in_text():
    text = "Login to http://localhost:8000?user=admin&password=SuperSecretPassword123!"
    masked = mask_text(text)
    assert "password=[REDACTED]" in masked
    assert "SuperSecretPassword123!" not in masked


def test_mask_tokens_and_api_keys():
    api_key_text = "Key: AIzaSyD9x8c7V6b5N4m3L2k1J0h9G8f7E6d5C4b"
    masked = mask_text(api_key_text)
    assert "[API_KEY_REDACTED]" in masked
    assert "AIzaSyD9x8c7V6b5N4m3L2k1J0h9G8f7E6d5C4b" not in masked


def test_mask_national_ids():
    pan_text = "PAN number is ABCDE1234F for taxation."
    masked = mask_text(pan_text)
    assert "[PAN_REDACTED]" in masked
    assert "ABCDE1234F" not in masked

    aadhaar_text = "Aadhaar: 1234 5678 9012 registered."
    masked_aadhaar = mask_text(aadhaar_text)
    assert "[AADHAAR_REDACTED]" in masked_aadhaar
    assert "1234 5678 9012" not in masked_aadhaar


def test_mask_dict_sensitive_keys():
    payload = {
        "username": "tester1",
        "password": "ClearTextPassword!",
        "access_token": "eyJhbGciOi...",
        "profile": {
            "email": "tester@company.org",
            "pin": 1234,
            "bio": "Call me on 9123456789"
        }
    }
    masked = mask_dict(payload)
    assert masked["password"] == "[REDACTED]"
    assert masked["access_token"] == "[REDACTED]"
    assert masked["profile"]["pin"] == "[REDACTED]"
    assert masked["profile"]["email"] == "[EMAIL_REDACTED]"
    assert "[PHONE_REDACTED]" in masked["profile"]["bio"]
    # Check original dict wasn't mutated
    assert payload["password"] == "ClearTextPassword!"


def test_mask_headers():
    headers = {
        "Authorization": "Bearer some-super-secret-token",
        "Cookie": "session_id=xyz987",
        "Content-Type": "application/json",
        "X-Custom": "SafeValue",
    }
    cleaned = mask_headers(headers)
    assert cleaned["Authorization"] == "[REDACTED]"
    assert cleaned["Cookie"] == "[REDACTED]"
    assert cleaned["Content-Type"] == "application/json"
    assert cleaned["X-Custom"] == "SafeValue"
