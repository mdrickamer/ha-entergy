"""The Entergy integration."""

from __future__ import annotations

import logging

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EntergyApiClient, EntergyApiError, EntergyAuthError, EntergyMfaRequired
from .const import (
    CONF_ACCOUNT_ID,
    CONF_LANGUAGE,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    friendly_account_name,
    normalize_scan_interval_seconds,
)
from .coordinator import EntergyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


def get_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> EntergyDataUpdateCoordinator:
    """Return coordinator for an entry."""
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


def _prepared_entry_updates(
    entry: ConfigEntry,
) -> tuple[int, dict[str, object]]:
    """Return the safe poll interval and any entry fields that should be migrated."""
    account_id = str(entry.data[CONF_ACCOUNT_ID])
    raw_interval = entry.options.get(CONF_SCAN_INTERVAL_SECONDS, DEFAULT_SCAN_INTERVAL_SECONDS)
    scan_interval = normalize_scan_interval_seconds(raw_interval)
    updates: dict[str, object] = {}

    if raw_interval != scan_interval:
        updates["options"] = {**entry.options, CONF_SCAN_INTERVAL_SECONDS: scan_interval}
        _LOGGER.info(
            "Entergy poll interval is outside the allowed range; using %s seconds",
            scan_interval,
        )

    if entry.title in {f"Entergy {account_id}", account_id}:
        updates["title"] = friendly_account_name(account_id)

    return scan_interval, updates


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Entergy from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    scan_interval, updates = _prepared_entry_updates(entry)
    if updates:
        hass.config_entries.async_update_entry(entry, **updates)

    session = async_get_clientsession(hass)
    client = EntergyApiClient(
        session=session,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        language=entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        extra_secrets=(str(entry.data[CONF_ACCOUNT_ID]),),
    )

    try:
        await client.async_initialize()
        await client.async_login()
    except EntergyMfaRequired as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EntergyAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (EntergyApiError, ClientError, TimeoutError) as err:
        _LOGGER.debug("Entergy setup cannot connect: %s", client.redact(err))
        raise ConfigEntryNotReady("Could not connect to Entergy") from None

    coordinator = EntergyDataUpdateCoordinator(
        hass=hass,
        client=client,
        account_id=str(entry.data[CONF_ACCOUNT_ID]),
        entry_id=entry.entry_id,
        config_entry=entry,
        scan_interval_seconds=scan_interval,
    )
    await coordinator.async_initialize()
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"client": client, "coordinator": coordinator}

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Entergy config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if entry_data:
            client: EntergyApiClient = entry_data["client"]
            await client.async_logout()
    return unload_ok
