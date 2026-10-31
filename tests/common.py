"""Pytest fixtures for the Entergy integration.

HTTP is mocked for every test. Nothing in this suite may contact Entergy.
"""

from __future__ import annotations

import json
import re
from typing import Any

from custom_components.entergy_mobile.const import (
    BASE_URL,
    CONF_ACCOUNT_ID,
    CONF_LANGUAGE,
    CONF_SCAN_INTERVAL_SECONDS,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

USERNAME = "home.user@example.com"
PASSWORD = "s3cret-password"
NEW_PASSWORD = "replacement-password"
ACCOUNT_ID = "100200300400"
OTHER_ACCOUNT_ID = "999888777666"
TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMDAyMDAzMDA0MDAifQ"
    ".signaturevalue"
)
CLIENT_ID = "client-secret-id"
ADDRESS = "123 Secret Maple Street"
BODY_MARKER = "outage-detail-marker"

def _endpoint(path: str) -> re.Pattern[str]:
    """Match one API path on the configured base URL, with or without a query."""
    return re.compile(re.escape(BASE_URL) + path)


APP_RE = _endpoint(r"/app(?:\?|$)")
LOGIN_RE = _endpoint(r"/login(?:\?|$)")
LOGOUT_RE = _endpoint(r"/logout(?:\?|$)")
ACCOUNTS_RE = _endpoint(r"/accounts(?:\?|$)")
USAGE_RE = _endpoint(r"/accounts/.+/weeklyusage")
OLD_HOST = "prod.entergy.mindgrb.io"

SECRETS = (PASSWORD, NEW_PASSWORD, USERNAME, ACCOUNT_ID, OTHER_ACCOUNT_ID, TOKEN, CLIENT_ID, ADDRESS)


def usage_payload(kwh: float = 1.25) -> dict[str, Any]:
    """Return a minimal weekly usage payload the coordinator can parse."""
    return {
        "data": {
            "daily": {
                "electric": [
                    {
                        "hourly": [
                            {
                                "date": "2026-10-29T15:00:00Z",
                                "usage": kwh,
                                "cost": 0.4,
                                "isEstimated": False,
                            }
                        ]
                    }
                ]
            },
            "weekly": {"electric": [{"usage": kwh, "cost": 0.4}]},
            "monthly": {"electric": [{"usage": kwh, "cost": 0.4, "date": "2026-10-01"}]},
        }
    }


def secret_body(marker: str = BODY_MARKER) -> dict[str, Any]:
    """Return an error body full of values that must not reach the logs."""
    return {
        "message": marker,
        "password": PASSWORD,
        "email": USERNAME,
        "accountNumber": ACCOUNT_ID,
        "accessToken": TOKEN,
        "clientId": CLIENT_ID,
        "serviceAddress": ADDRESS,
    }


def _json_body(kwargs: dict[str, Any]) -> dict[str, Any]:
    body = kwargs.get("json")
    if isinstance(body, dict):
        return body
    data = kwargs.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, (bytes, str)):
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


class FakeResponse:
    """Minimal aiohttp response stand-in.

    aiohttp 3.14 awaits ``ClientResponse.__aenter__`` after ``_request`` returns.
    """

    def __init__(self, status: int = 200, payload: Any = None) -> None:
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._payload = {} if payload is None else payload

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def close(self) -> None:
        return None

    async def json(self, content_type: Any = None) -> Any:
        return self._payload

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)


class EntergyFakeApi:
    """Scripted Entergy HTTP API. Unmatched calls raise instead of using the network."""

    def __init__(self) -> None:
        self.login_status = 200
        self.login_exception: Exception | None = None
        self.mfa = False
        self.accepted_passwords = {PASSWORD}
        self.usage_status = 200
        self.usage_exception: Exception | None = None
        self.usage_body: dict[str, Any] | None = None
        self.app_status = 200
        self.app_exception: Exception | None = None
        self.accounts_payload: dict[str, Any] | None = None
        self.login_calls = 0
        self.usage_calls = 0
        self.app_calls = 0
        self.usage_urls: list[str] = []
        self.seen: list[tuple[str, str]] = []
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        """Route one mocked Entergy request. Anything else is a test failure."""
        method = method.upper()
        self.seen.append((method, url))
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "headers": kwargs.get("headers"),
            }
        )
        if "mindgrb" in url.lower():
            raise AssertionError(f"Refusing the old Entergy host: {method} {url}")
        if not url.startswith(BASE_URL):
            raise AssertionError(f"Refusing unexpected Entergy URL: {method} {url}")
        if method == "GET" and APP_RE.match(url):
            return self.app_callback(url, **kwargs)
        if method == "POST" and LOGIN_RE.match(url):
            return self.login_callback(url, **kwargs)
        if method == "GET" and ACCOUNTS_RE.match(url):
            return self.accounts_callback(url, **kwargs)
        if method == "GET" and USAGE_RE.match(url):
            return self.usage_callback(url, **kwargs)
        if method == "POST" and LOGOUT_RE.match(url):
            return FakeResponse(200, {"ok": True})
        raise AssertionError(f"Unmocked Entergy request: {method} {url}")

    def app_callback(self, url: Any, **kwargs: Any) -> FakeResponse:
        self.app_calls += 1
        if self.app_exception is not None:
            raise self.app_exception
        if self.app_status >= 400:
            return FakeResponse(self.app_status, secret_body())
        return FakeResponse(200, {"clientId": CLIENT_ID})

    def login_callback(self, url: Any, **kwargs: Any) -> FakeResponse:
        self.login_calls += 1
        if self.login_exception is not None:
            raise self.login_exception
        if self.login_status != 200:
            return FakeResponse(self.login_status, secret_body())
        password = _json_body(kwargs).get("password")
        if password not in self.accepted_passwords:
            return FakeResponse(401, secret_body("invalid credentials"))
        if self.mfa:
            return FakeResponse(200, {"data": {"nextAction": "VERIFY_MFA", "email": USERNAME}})
        return FakeResponse(200, {"data": {"accessToken": TOKEN}})

    def accounts_callback(self, url: Any, **kwargs: Any) -> FakeResponse:
        payload = self.accounts_payload or {
            "data": [
                {
                    "accountNumber": ACCOUNT_ID,
                    "serviceAddress": ADDRESS,
                    "email": USERNAME,
                }
            ]
        }
        return FakeResponse(200, payload)

    def usage_callback(self, url: Any, **kwargs: Any) -> FakeResponse:
        self.usage_calls += 1
        self.usage_urls.append(str(url))
        if self.usage_exception is not None:
            raise self.usage_exception
        if self.usage_status >= 400:
            return FakeResponse(self.usage_status, secret_body())
        body = self.usage_body if self.usage_body is not None else usage_payload()
        return FakeResponse(200, body)


def assert_no_secrets(text: str) -> None:
    """Fail if a credential or account identifier leaked into text."""
    for secret in SECRETS:
        assert secret not in text


def make_entry(
    *,
    scan_interval: int = 3600,
    password: str = PASSWORD,
    account_id: str = ACCOUNT_ID,
    title: str = "Entergy",
) -> MockConfigEntry:
    """Return a config entry that has not been added to Home Assistant yet."""
    return MockConfigEntry(
        domain="entergy_mobile",
        title=title,
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: password,
            CONF_LANGUAGE: "en",
            CONF_ACCOUNT_ID: account_id,
        },
        options={CONF_SCAN_INTERVAL_SECONDS: scan_interval},
        unique_id=f"{USERNAME}_{account_id}",
    )


async def async_setup_entry(hass, entry: MockConfigEntry):
    """Add an entry and run setup."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
