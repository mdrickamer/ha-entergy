"""Redaction helpers for logs and diagnostics."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "**REDACTED**"

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "username",
    "email",
    "emailaddress",
    "email_address",
    "account",
    "accountid",
    "account_id",
    "accountnumber",
    "account_number",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "token",
    "clientid",
    "client_id",
    "authorization",
    "serviceaddress",
    "service_address",
    "premiseaddress",
    "premise_address",
    "mailingaddress",
    "mailing_address",
    "address",
    "street",
    "phone",
    "phonenumber",
    "phone_number",
}

_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
_ACCOUNT_PATH_RE = re.compile(r"(/accounts/)([^/?#\s\"']+)")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_?token|refresh_?token|token|password|username|email|"
    r"account(?:_?id|number)?|client_?id)=)[^&#\s]+"
)
_JSON_SECRET_STRING_RE = re.compile(
    r'(?i)("(?:password|passwd|username|email|email_?address|account(?:_?id|number)|'
    r'access_?token|refresh_?token|token|client_?id|authorization|service_?address|'
    r'premise_?address|mailing_?address|address|phone|phone_?number)"\s*:\s*")'
    r'([^"\\]*)(")'
)
_JSON_SECRET_NUMBER_RE = re.compile(
    r'(?i)("(?:account(?:_?id|number)|phone|phone_?number)"\s*:\s*)(-?\d+)'
)
_LONG_NUMBER_RE = re.compile(r"\d{7,}")


def _key_is_sensitive(key: Any) -> bool:
    return str(key).replace("-", "").replace("_", "").lower() in _SENSITIVE_KEYS or str(key).lower() in _SENSITIVE_KEYS


def _redact_structure(value: Any, secrets: Iterable[str] | None, *, mask_long_numbers: bool) -> Any:
    """Redact mappings before they are converted to text."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _key_is_sensitive(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact_structure(item, secrets, mask_long_numbers=mask_long_numbers)
        return redacted
    if isinstance(value, list):
        return [_redact_structure(item, secrets, mask_long_numbers=mask_long_numbers) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets, mask_long_numbers=mask_long_numbers, _structured=True)
    return value


def _stringify(value: Any) -> str:
    """Return a log-safe string without calling a custom __repr__ that might leak."""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    return str(value)


def redact_text(
    value: Any,
    secrets: Iterable[str] | None = None,
    *,
    mask_long_numbers: bool = True,
    _structured: bool = False,
) -> str:
    """Remove credentials, account identifiers, emails, and tokens from text."""
    if not _structured and isinstance(value, (dict, list)):
        value = _redact_structure(value, secrets, mask_long_numbers=mask_long_numbers)
    text = _stringify(value)
    if secrets:
        for secret in sorted({item for item in secrets if item and len(item) >= 4}, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
    text = _EMAIL_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _JSON_SECRET_STRING_RE.sub(rf"\1{REDACTED}\3", text)
    text = _JSON_SECRET_NUMBER_RE.sub(rf"\1{REDACTED}", text)
    text = _ACCOUNT_PATH_RE.sub(rf"\1{REDACTED}", text)
    text = _QUERY_SECRET_RE.sub(rf"\1{REDACTED}", text)
    if mask_long_numbers:
        text = _LONG_NUMBER_RE.sub(REDACTED, text)
    return text


def scrub_structure(
    data: Any,
    secrets: Iterable[str] | None = None,
    *,
    mask_long_numbers: bool = False,
) -> Any:
    """Redact secrets inside nested diagnostic values."""
    if isinstance(data, str):
        return redact_text(data, secrets, mask_long_numbers=mask_long_numbers)
    if isinstance(data, dict):
        scrubbed: dict[Any, Any] = {}
        for key, value in data.items():
            if _key_is_sensitive(key):
                scrubbed[key] = REDACTED
            else:
                scrubbed[key] = scrub_structure(value, secrets, mask_long_numbers=mask_long_numbers)
        return scrubbed
    if isinstance(data, list):
        return [scrub_structure(item, secrets, mask_long_numbers=mask_long_numbers) for item in data]
    return data
