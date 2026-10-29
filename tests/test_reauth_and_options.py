"""Reauthentication and poll-interval option tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er

from custom_components.entergy_mobile.const import CONF_SCAN_INTERVAL_SECONDS, DOMAIN
from homeassistant.const import CONF_PASSWORD

from tests.common import (
    ACCOUNT_ID,
    NEW_PASSWORD,
    OTHER_ACCOUNT_ID,
    PASSWORD,
    async_setup_entry,
    make_entry,
)


def _entity_ids(hass, entry_id: str) -> list[str]:
    registry = er.async_get(hass)
    return sorted(entry.entity_id for entry in er.async_entries_for_config_entry(registry, entry_id))


async def test_reauth_updates_password_and_keeps_statistics(hass, entergy_api) -> None:
    """A new password is saved on the same entry and cumulative totals survive."""
    entry = await async_setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.LOADED
    entry_id = entry.entry_id
    entities = _entity_ids(hass, entry_id)
    assert entities
    total = hass.data[DOMAIN][entry_id]["coordinator"].data["total_import_kwh"]
    assert total == 1.25

    entergy_api.accepted_passwords = set()
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    rejected = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: "wrong-password-value"},
    )
    assert rejected["type"] is FlowResultType.FORM
    assert rejected["errors"]["base"] == "invalid_auth"
    assert entry.data[CONF_PASSWORD] == PASSWORD

    entergy_api.accepted_passwords = {NEW_PASSWORD}
    updated = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: NEW_PASSWORD},
    )
    assert updated["type"] is FlowResultType.ABORT
    assert updated["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert entry.entry_id == entry_id
    assert entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert entry.state is ConfigEntryState.LOADED
    assert _entity_ids(hass, entry_id) == entities
    assert hass.data[DOMAIN][entry_id]["coordinator"].data["total_import_kwh"] == total


async def test_reauth_from_failed_setup_reloads_same_entry(hass, entergy_api) -> None:
    """Reauth after a startup login failure reloads the original entry."""
    entergy_api.login_status = 401
    entry = await async_setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.SETUP_ERROR
    entry_id = entry.entry_id
    progress = hass.config_entries.flow.async_progress()
    assert progress
    flow_id = progress[0]["flow_id"]

    entergy_api.login_status = 200
    entergy_api.accepted_passwords = {NEW_PASSWORD}
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_PASSWORD: NEW_PASSWORD})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert entry.entry_id == entry_id
    assert entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry_id]["coordinator"].data["total_import_kwh"] == 1.25


async def test_reauth_rejects_a_different_account(hass, entergy_api) -> None:
    """A password for a different Entergy account does not replace the saved one."""
    entry = await async_setup_entry(hass, make_entry())
    entergy_api.accepted_passwords = {NEW_PASSWORD}
    entergy_api.accounts_payload = {"data": [{"accountNumber": OTHER_ACCOUNT_ID}]}

    result = await entry.start_reauth_flow(hass)
    rejected = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: NEW_PASSWORD},
    )
    assert rejected["type"] is FlowResultType.FORM
    assert rejected["errors"]["base"] == "account_missing"
    assert entry.data[CONF_PASSWORD] == PASSWORD
    assert entry.state is ConfigEntryState.LOADED


async def test_options_flow_enforces_minimum_interval(hass, entergy_api) -> None:
    """The options form rejects intervals below 900 seconds and accepts longer ones."""
    entry = await async_setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    with pytest.raises(InvalidData) as err:
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL_SECONDS: 60},
        )
    assert "scan_interval_seconds" in err.value.schema_errors
    assert entry.options[CONF_SCAN_INTERVAL_SECONDS] == 3600

    saved = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL_SECONDS: 1800},
    )
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_SCAN_INTERVAL_SECONDS] == 1800
    assert hass.data[DOMAIN][entry.entry_id]["coordinator"].update_interval == timedelta(seconds=1800)
    assert ACCOUNT_ID not in entry.title
