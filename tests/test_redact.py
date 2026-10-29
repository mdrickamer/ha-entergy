"""Redaction unit tests."""

import pytest

from custom_components.entergy_mobile.const import (
    BASE_URL,
    DEFAULT_APP_VERSION,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    LOGIN_URL,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    friendly_account_name,
    normalize_scan_interval_seconds,
)
from custom_components.entergy_mobile.redact import REDACTED, redact_text
from tests.common import ACCOUNT_ID, ADDRESS, CLIENT_ID, OLD_HOST, PASSWORD, TOKEN, USERNAME, EntergyFakeApi


def test_api_host_and_app_version() -> None:
    """The client uses the official mobile API host and the current app version."""
    assert BASE_URL == "https://mobileapp.api-prod.entergy.com/api"
    assert "mindgrb" not in BASE_URL
    assert LOGIN_URL == f"{BASE_URL}/login"
    assert DEFAULT_APP_VERSION == "3.63.0"


def test_fake_api_refuses_the_old_host() -> None:
    """The old Mindgrub host is rejected inside the fake and is not a passthrough."""
    api = EntergyFakeApi()
    with pytest.raises(AssertionError, match="old Entergy host"):
        api.dispatch("GET", f"https://{OLD_HOST}/api/app", {})
    assert api.seen


def test_poll_interval_bounds() -> None:
    """The default stays hourly and the minimum is at least 15 minutes."""
    assert DEFAULT_SCAN_INTERVAL_SECONDS == 3600
    assert MIN_SCAN_INTERVAL_SECONDS >= 900
    assert MAX_SCAN_INTERVAL_SECONDS == 86400
    assert normalize_scan_interval_seconds(60) == MIN_SCAN_INTERVAL_SECONDS
    assert normalize_scan_interval_seconds(900) == 900
    assert normalize_scan_interval_seconds(3600) == 3600
    assert normalize_scan_interval_seconds(90000) == MAX_SCAN_INTERVAL_SECONDS
    assert normalize_scan_interval_seconds("nope") == DEFAULT_SCAN_INTERVAL_SECONDS
    assert normalize_scan_interval_seconds(None) == DEFAULT_SCAN_INTERVAL_SECONDS


def test_friendly_name_hides_full_account_number() -> None:
    """Entry labels keep only the last four characters of an account number."""
    label = friendly_account_name(ACCOUNT_ID)
    assert ACCOUNT_ID not in label
    assert label == "Entergy ending 0400"
    assert friendly_account_name("123") == "Entergy"


def test_redact_text_removes_credentials_and_account_ids() -> None:
    """Secrets are removed even when they are embedded in a response body."""
    raw = (
        f"password={PASSWORD} email={USERNAME} account /accounts/{ACCOUNT_ID}/weeklyusage "
        f"token={TOKEN} client={CLIENT_ID} address={ADDRESS} "
        '{"accountNumber": "100200300400", "accessToken": "plain-token-value"}'
    )
    redacted = redact_text(raw, secrets=(PASSWORD, CLIENT_ID, ADDRESS, "plain-token-value"))
    for secret in (PASSWORD, USERNAME, ACCOUNT_ID, TOKEN, CLIENT_ID, ADDRESS, "plain-token-value", "100200300400"):
        assert secret not in redacted
    assert REDACTED in redacted
