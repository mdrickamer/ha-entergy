"""Diagnostics support for Entergy."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import get_coordinator
from .const import CONF_ACCOUNT_ID
from .redact import scrub_structure

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ACCOUNT_ID,
    "account_id",
    "accountId",
    "accountID",
    "accountNumber",
    "account_number",
    "access_token",
    "accessToken",
    "refresh_token",
    "refreshToken",
    "token",
    "Authorization",
    "authorization",
    "clientId",
    "client_id",
    "raw",
    "email",
    "emailAddress",
    "email_address",
    "username",
    "password",
    "serviceAddress",
    "service_address",
    "premiseAddress",
    "premise_address",
    "mailingAddress",
    "mailing_address",
    "address",
    "street",
    "phone",
    "phoneNumber",
    "phone_number",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = get_coordinator(hass, entry)
    data = {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
        "coordinator_data": coordinator.data,
    }
    redacted = async_redact_data(data, TO_REDACT)
    secrets = [
        str(entry.data.get(CONF_USERNAME) or ""),
        str(entry.data.get(CONF_PASSWORD) or ""),
        str(entry.data.get(CONF_ACCOUNT_ID) or ""),
    ]
    if entry.unique_id:
        secrets.append(entry.unique_id)
    return scrub_structure(redacted, secrets)
