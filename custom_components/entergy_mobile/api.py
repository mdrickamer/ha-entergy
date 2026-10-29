"""API client for Entergy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import logging
from typing import Any

import aiohttp

from .const import (
    ACCOUNTS_URL,
    APP_CONFIG_URL,
    BASE_URL,
    DEFAULT_APP_VERSION,
    DEFAULT_LANGUAGE,
    LOGIN_URL,
    LOGOUT_URL,
    WEEKLY_USAGE_URL,
)
from .redact import redact_text

_LOGGER = logging.getLogger(__name__)


class EntergyApiError(Exception):
    """Base Entergy API error."""


class EntergyAuthError(EntergyApiError):
    """Authentication failed or expired."""


class EntergyMfaRequired(EntergyAuthError):
    """The API requested another authentication action that is not supported."""


@dataclass
class EntergyLoginResult:
    """Login result."""

    access_token: str
    raw: dict[str, Any]


class EntergyApiClient:
    """Small async client for the Entergy mobile API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        app_version: str = DEFAULT_APP_VERSION,
        language: str = DEFAULT_LANGUAGE,
        timeout_seconds: int = 30,
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._app_version = app_version
        self._language = language
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._extra_secrets = tuple(secret for secret in extra_secrets if secret)
        self._client_id: str | None = None
        self._access_token: str | None = None

    @property
    def client_id(self) -> str | None:
        """Return the current client ID."""
        return self._client_id

    @property
    def access_token(self) -> str | None:
        """Return the current access token."""
        return self._access_token

    def secrets(self) -> tuple[str, ...]:
        """Return values that must never be written to logs."""
        values = (
            self._username,
            self._password,
            self._access_token,
            self._client_id,
            *self._extra_secrets,
        )
        return tuple(value for value in values if value)

    def redact(self, value: Any) -> str:
        """Redact secrets from a value before it is logged."""
        return redact_text(value, self.secrets())

    async def async_initialize(self) -> None:
        """Initialize client metadata required by the Entergy API."""
        self._client_id = await self.async_get_client_id()

    async def async_get_client_id(self) -> str:
        """Fetch clientId from /api/app."""
        _LOGGER.debug("Fetching Entergy app config")
        status, body = await self._send("GET", APP_CONFIG_URL)
        if status >= 400:
            self._debug_response("app config", status, body)
            raise EntergyApiError(f"Could not fetch app config: HTTP {status}")
        if not isinstance(body, dict):
            raise EntergyApiError("Unexpected app config response")

        client_id = body.get("clientId")
        if not client_id:
            data = body.get("data")
            if isinstance(data, dict):
                client_id = data.get("clientId")
        if not client_id:
            self._debug_response("app config", status, body)
            raise EntergyApiError("clientId missing from /api/app response")
        _LOGGER.debug("Fetched Entergy app config with clientId present")
        return str(client_id)

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "appVersion": self._app_version,
            "language": self._language,
        }
        if extra:
            params.update(extra)
        return params

    def _headers(self, token: str | None = None) -> dict[str, str]:
        if not self._client_id:
            raise EntergyApiError("Client not initialized")
        bearer = token if token is not None else self._access_token
        if not bearer:
            bearer = "0"
        return {
            "Accept": "application/json",
            "clientId": self._client_id,
            "Authorization": f"Bearer {bearer}",
        }

    async def _read_body(self, resp: aiohttp.ClientResponse) -> Any:
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            return await resp.json(content_type=None)
        text = await resp.text()
        try:
            return await resp.json(content_type=None)
        except Exception:
            return text

    def _debug_response(self, action: str, status: int, body: Any) -> None:
        """Log a response body at debug level with secrets removed."""
        _LOGGER.debug("Entergy %s response HTTP %s: %s", action, status, self.redact(body))

    async def _send(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        """Perform one HTTP request and hide network details from raised errors."""
        _LOGGER.debug("Requesting Entergy API: %s %s", method, self.redact(url))
        try:
            async with self._session.request(method, url, timeout=self._timeout, **kwargs) as resp:
                body = await self._read_body(resp)
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Entergy network error during %s: %s", method, self.redact(err))
            raise EntergyApiError("Could not reach Entergy") from None
        return status, body

    async def async_login(self) -> EntergyLoginResult:
        """Login and store the returned access token."""
        if not self._client_id:
            await self.async_initialize()

        payload = {"username": self._username, "password": self._password}
        _LOGGER.debug("Logging in to Entergy API")

        status, body = await self._send(
            "POST",
            LOGIN_URL,
            params=self._params(),
            headers={**self._headers("0"), "Content-Type": "application/json"},
            json=payload,
        )

        if status in (401, 403):
            self._debug_response("login", status, body)
            raise EntergyAuthError(f"Login failed: HTTP {status}")
        if status >= 400:
            self._debug_response("login", status, body)
            raise EntergyApiError(f"Login failed: HTTP {status}")
        if not isinstance(body, dict):
            raise EntergyApiError("Unexpected login response")

        login_data = body.get("data")
        if not isinstance(login_data, dict):
            login_data = body

        next_action = login_data.get("nextAction") or body.get("nextAction")
        if next_action:
            _LOGGER.debug("Entergy login next action: %s", self.redact(next_action))
            raise EntergyMfaRequired("Login requires an unsupported additional authentication step")

        token = (
            login_data.get("accessToken")
            or login_data.get("access_token")
            or login_data.get("token")
            or body.get("accessToken")
            or body.get("access_token")
            or body.get("token")
        )
        if not token:
            self._debug_response("login", status, body)
            raise EntergyAuthError("Login response did not contain an access token")

        self._access_token = str(token)
        _LOGGER.debug("Entergy API login succeeded")
        return EntergyLoginResult(access_token=self._access_token, raw=body)

    async def async_logout(self) -> None:
        """Logout best-effort."""
        if not self._client_id:
            return
        try:
            await self._send(
                "POST",
                LOGOUT_URL,
                params=self._params(),
                headers=self._headers(),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Logout failed: %s", self.redact(err))

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        if not self._client_id:
            await self.async_initialize()
        if not self._access_token:
            await self.async_login()

        status, body = await self._send(
            method,
            url,
            params=self._params(params),
            headers=self._headers(),
        )

        if status == 401 and retry_auth:
            _LOGGER.debug("Entergy API returned 401; refreshing login and retrying once")
            await self.async_login()
            return await self._request_json(method, url, params=params, retry_auth=False)

        if status in (401, 403):
            self._debug_response("request", status, body)
            raise EntergyAuthError(f"Unauthorized: HTTP {status}")
        if status >= 400:
            self._debug_response("request", status, body)
            raise EntergyApiError(f"API request failed: HTTP {status}")
        return body

    async def async_get_accounts(self) -> Any:
        """Fetch the user's accounts."""
        return await self._request_json("GET", ACCOUNTS_URL)

    async def async_get_account(self, account_id: str) -> Any:
        """Fetch one account."""
        return await self._request_json("GET", f"{ACCOUNTS_URL}/{account_id}")

    async def async_get_weekly_usage(
        self,
        account_id: str,
        start_date: date,
        view: str = "day",
    ) -> Any:
        """Fetch weekly usage payload for an account."""
        url = WEEKLY_USAGE_URL.format(account_id=account_id)
        return await self._request_json(
            "GET",
            url,
            params={"view": view, "startDate": start_date.isoformat()},
        )

    async def async_fetch_current_usage(self, account_id: str) -> Any:
        """Fetch the latest usage window."""
        today = date.today()
        return await self.async_get_weekly_usage(
            account_id=account_id,
            start_date=today - timedelta(days=6),
            view="day",
        )

    def absolute_url(self, path: str) -> str:
        """Return an absolute API URL for diagnostics/tests."""
        path = path.lstrip("/")
        return f"{BASE_URL}/{path}"
