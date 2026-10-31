"""Constants for the Entergy integration."""

from __future__ import annotations

from typing import Any, Final

DOMAIN: Final = "entergy_mobile"

CONF_ACCOUNT_ID: Final = "account_id"
CONF_LANGUAGE: Final = "language"
CONF_SCAN_INTERVAL_SECONDS: Final = "scan_interval_seconds"

DEFAULT_LANGUAGE: Final = "en"
DEFAULT_APP_VERSION: Final = "3.63.0"
DEFAULT_SCAN_INTERVAL_SECONDS: Final = 3600
MIN_SCAN_INTERVAL_SECONDS: Final = 900
MAX_SCAN_INTERVAL_SECONDS: Final = 86400

BASE_URL: Final = "https://mobileapp.api-prod.entergy.com/api"
APP_CONFIG_URL: Final = f"{BASE_URL}/app"
LOGIN_URL: Final = f"{BASE_URL}/login"
LOGOUT_URL: Final = f"{BASE_URL}/logout"
ACCOUNTS_URL: Final = f"{BASE_URL}/accounts"
WEEKLY_USAGE_URL: Final = f"{BASE_URL}/accounts/{{account_id}}/weeklyusage"

STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = "entergy_mobile_usage"

ATTR_ACCOUNT_ID: Final = "account_id"
ATTR_LAST_UPDATE: Final = "last_update"
ATTR_LAST_INTERVAL: Final = "last_interval"
ATTR_IS_ESTIMATED: Final = "is_estimated"
ATTR_HOUR_START: Final = "hour_start"

# Re-import this many days of each weeklyusage response so Entergy can correct
# hours it has already sent. Older imported hours stay in the recorder.
STATISTICS_WINDOW_DAYS: Final = 4
ACCOUNT_TIME_ZONE: Final = "America/Chicago"


def normalize_scan_interval_seconds(value: Any) -> int:
    """Return a poll interval inside the allowed range.

    Existing entries may have stored a value below the current minimum.
    Those are raised to the minimum instead of being rejected.
    """
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL_SECONDS
    if interval < MIN_SCAN_INTERVAL_SECONDS:
        return MIN_SCAN_INTERVAL_SECONDS
    if interval > MAX_SCAN_INTERVAL_SECONDS:
        return MAX_SCAN_INTERVAL_SECONDS
    return interval


def friendly_account_name(account_id: str) -> str:
    """Return an entry label that does not include the full account number."""
    text = str(account_id).strip()
    if len(text) <= 4:
        return "Entergy"
    return f"Entergy ending {text[-4:]}"
