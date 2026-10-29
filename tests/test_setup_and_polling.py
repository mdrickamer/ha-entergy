"""Setup, authentication lockout, and transient retry tests."""

from __future__ import annotations

from datetime import timedelta
import logging

import aiohttp
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.entergy_mobile.const import DOMAIN

from tests.common import (
    ACCOUNT_ID,
    BODY_MARKER,
    USERNAME,
    assert_no_secrets,
    async_setup_entry,
    make_entry,
)


def _coordinator(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


def _reauth_flows(hass, entry_id: str) -> list[dict]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth" and flow["context"].get("entry_id") == entry_id
    ]


async def test_requests_use_the_mobile_api_host(hass, entergy_api) -> None:
    """Login reports app version 3.63.0 and does not send a platform header."""
    await async_setup_entry(hass, make_entry())

    assert entergy_api.seen
    for _method, url in entergy_api.seen:
        assert url.startswith("https://mobileapp.api-prod.entergy.com/api")
        assert "mindgrb" not in url.lower()

    logins = [call for call in entergy_api.calls if call["url"].endswith("/login")]
    assert logins
    assert logins[0]["params"]["appVersion"] == "3.63.0"
    assert "platform" not in logins[0]["params"]
    header_names = {str(name).lower() for name in (logins[0]["headers"] or {})}
    assert "platform" not in header_names

    usage = [call for call in entergy_api.calls if "weeklyusage" in call["url"]]
    assert usage
    assert usage[0]["params"]["view"] == "day"
    assert "startDate" in usage[0]["params"]
    assert "platform" not in usage[0]["params"]


async def test_auth_failure_stops_polling_and_starts_reauth(hass, entergy_api, caplog) -> None:
    """A rejected password starts reauth and does not schedule another poll."""
    caplog.set_level(logging.DEBUG, logger="custom_components.entergy_mobile")
    entry = await async_setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.LOADED
    coordinator = _coordinator(hass, entry)
    assert coordinator.data["total_import_kwh"] == 1.25

    login_calls = entergy_api.login_calls
    usage_calls = entergy_api.usage_calls
    entergy_api.usage_status = 401
    entergy_api.login_status = 401

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert coordinator.update_interval is None
    assert coordinator._unsub_refresh is None
    assert coordinator.last_update_success is False
    assert _reauth_flows(hass, entry.entry_id)
    assert entergy_api.login_calls == login_calls + 1
    assert entergy_api.usage_calls == usage_calls + 1
    assert_no_secrets(caplog.text)
    assert BODY_MARKER not in "\n".join(
        record.getMessage() for record in caplog.records if record.levelno > logging.DEBUG
    )


async def test_transient_failure_retries_with_backoff(hass, entergy_api, caplog) -> None:
    """Server errors stay on the normal retry path and do not ask for reauth."""
    caplog.set_level(logging.DEBUG, logger="custom_components.entergy_mobile")
    entry = await async_setup_entry(hass, make_entry())
    coordinator = _coordinator(hass, entry)
    configured = coordinator.update_interval

    login_calls = entergy_api.login_calls
    entergy_api.usage_status = 503
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert not _reauth_flows(hass, entry.entry_id)
    assert coordinator.update_interval == timedelta(seconds=configured.total_seconds() * 2)
    assert coordinator._unsub_refresh is not None
    assert entergy_api.login_calls == login_calls

    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=configured.total_seconds() * 4)

    entergy_api.usage_status = 200
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.update_interval == configured
    assert_no_secrets(caplog.text)
    visible = "\n".join(record.getMessage() for record in caplog.records if record.levelno > logging.DEBUG)
    debug = "\n".join(record.getMessage() for record in caplog.records if record.levelno <= logging.DEBUG)
    assert BODY_MARKER not in visible
    assert "HTTP 503" in visible
    assert BODY_MARKER in debug


async def test_timeout_is_retried(hass, entergy_api) -> None:
    """Timeouts are transient and keep the poll schedule."""
    entry = await async_setup_entry(hass, make_entry())
    coordinator = _coordinator(hass, entry)
    entergy_api.usage_exception = TimeoutError(
        "timed out contacting https://mobileapp.api-prod.entergy.com/api/accounts/"
        f"{ACCOUNT_ID}/weeklyusage for {USERNAME}"
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert coordinator.update_interval is not None
    assert not _reauth_flows(hass, entry.entry_id)
    assert "Could not reach Entergy" in str(coordinator.last_exception)
    assert ACCOUNT_ID not in str(coordinator.last_exception)
    assert USERNAME not in str(coordinator.last_exception)


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientConnectionError("connection refused to mobileapp.api-prod.entergy.com"),
        TimeoutError("timed out"),
    ],
)
async def test_setup_network_failure_is_not_ready(hass, entergy_api, exc) -> None:
    """Startup cannot reach Entergy, so Home Assistant retries setup later."""
    entergy_api.app_exception = exc
    entry = await async_setup_entry(hass, make_entry())

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass, entry.entry_id)
    assert DOMAIN not in hass.data or entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_setup_server_error_is_not_ready(hass, entergy_api) -> None:
    """An Entergy 500 during setup is retried and does not start reauth."""
    entergy_api.login_status = 500
    entry = await async_setup_entry(hass, make_entry())

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass, entry.entry_id)
    assert entergy_api.login_calls == 1


async def test_setup_auth_failure_starts_reauth_once(hass, entergy_api, caplog) -> None:
    """A bad password at startup starts reauth and does not keep logging in."""
    caplog.set_level(logging.DEBUG, logger="custom_components.entergy_mobile")
    entergy_api.login_status = 401
    entry = await async_setup_entry(hass, make_entry())

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_flows(hass, entry.entry_id)
    assert entergy_api.login_calls == 1
    await hass.async_block_till_done()
    assert entergy_api.login_calls == 1
    assert_no_secrets(caplog.text)


async def test_setup_mfa_starts_reauth(hass, entergy_api) -> None:
    """An unsupported extra login step is an auth failure, not a retry loop."""
    entergy_api.mfa = True
    entry = await async_setup_entry(hass, make_entry())

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_flows(hass, entry.entry_id)
    assert entergy_api.login_calls == 1


async def test_short_poll_interval_and_title_are_migrated(hass, entergy_api) -> None:
    """Saved intervals below 900 seconds are raised, and the title drops the full account id."""
    entry = await async_setup_entry(
        hass,
        make_entry(scan_interval=60, title=f"Entergy {ACCOUNT_ID}"),
    )

    assert entry.state is ConfigEntryState.LOADED
    assert entry.options["scan_interval_seconds"] == 900
    assert entry.title == "Entergy ending 0400"
    assert ACCOUNT_ID not in entry.title
    assert _coordinator(hass, entry).update_interval == timedelta(seconds=900)

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, ACCOUNT_ID), entry.entry_id)
    assert device is not None
    assert device.name == "Entergy ending 0400"
    assert ACCOUNT_ID not in device.name
