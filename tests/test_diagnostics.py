"""Diagnostics redaction tests."""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED

from custom_components.entergy_mobile.const import CONF_ACCOUNT_ID
from custom_components.entergy_mobile.diagnostics import async_get_config_entry_diagnostics
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from tests.common import ACCOUNT_ID, ADDRESS, PASSWORD, TOKEN, USERNAME, async_setup_entry, make_entry


async def test_diagnostics_redact_identifiers(hass, entergy_api) -> None:
    """Account ids, emails, addresses, and credentials are removed from diagnostics."""
    entry = await async_setup_entry(hass, make_entry())
    coordinator = hass.data["entergy_mobile"][entry.entry_id]["coordinator"]
    coordinator.data = {
        **coordinator.data,
        "serviceAddress": ADDRESS,
        "email": USERNAME,
        "accountNumber": ACCOUNT_ID,
        "accessToken": TOKEN,
        "note": f"contact {USERNAME} about account {ACCOUNT_ID}",
    }

    result = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(result)

    assert result["entry"]["data"][CONF_PASSWORD] == REDACTED
    assert result["entry"]["data"][CONF_USERNAME] == REDACTED
    assert result["entry"]["data"][CONF_ACCOUNT_ID] == REDACTED
    assert result["coordinator_data"]["serviceAddress"] == REDACTED
    assert result["coordinator_data"]["email"] == REDACTED
    assert result["coordinator_data"]["accountNumber"] == REDACTED
    assert result["coordinator_data"]["accessToken"] == REDACTED
    for secret in (PASSWORD, USERNAME, ACCOUNT_ID, TOKEN, ADDRESS):
        assert secret not in dumped
    assert result["coordinator_data"]["total_import_kwh"] == 1.25
