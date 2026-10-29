"""Config flow for Entergy."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError

try:
    import probatio as vol
except ImportError:  # pragma: no cover - Home Assistant before probatio
    import voluptuous as vol  # type: ignore[no-redef]

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EntergyApiClient, EntergyApiError, EntergyAuthError, EntergyMfaRequired
from .const import (
    CONF_ACCOUNT_ID,
    CONF_LANGUAGE,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    friendly_account_name,
    normalize_scan_interval_seconds,
)
from .redact import redact_text

_LOGGER = logging.getLogger(__name__)


def _user_schema(default_language: str = DEFAULT_LANGUAGE) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_LANGUAGE, default=default_language): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "en", "label": "English"},
                        {"value": "es", "label": "Spanish"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _reauth_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_PASSWORD): str})


def _extract_accounts(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]

    if isinstance(raw, dict):
        for key in ("accounts", "data", "items", "results"):
            value = raw.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

        accounts = []
        for value in raw.values():
            if isinstance(value, dict):
                accounts.append(value)
        if accounts:
            return accounts

    return []


def _account_id(account: dict[str, Any]) -> str | None:
    for key in (
        "id",
        "accountId",
        "accountID",
        "account_id",
        "accountNumber",
        "account_number",
        "number",
    ):
        value = account.get(key)
        if value:
            return str(value)
    return None


def _account_label(account: dict[str, Any]) -> str:
    account_identifier = _account_id(account) or "Unknown"
    for key in ("nickname", "name", "serviceAddress", "address", "premiseAddress"):
        value = account.get(key)
        if value:
            return f"{account_identifier} - {value}"
    return account_identifier


async def _validate_and_fetch_accounts(
    hass: HomeAssistant,
    username: str,
    password: str,
    language: str,
    extra_secrets: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    session = async_get_clientsession(hass)
    client = EntergyApiClient(
        session=session,
        username=username,
        password=password,
        language=language,
        extra_secrets=extra_secrets,
    )
    await client.async_initialize()
    await client.async_login()
    raw = await client.async_get_accounts()
    return _extract_accounts(raw)


class EntergyMobileConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an Entergy config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._flow_data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle username/password step."""
        errors: dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_user_schema(), errors=errors)

        username = user_input[CONF_USERNAME]
        password = user_input[CONF_PASSWORD]
        language = user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)

        try:
            accounts = await _validate_and_fetch_accounts(self.hass, username, password, language)
        except EntergyMfaRequired as err:
            _LOGGER.debug("Entergy setup requires unsupported MFA step: %s", redact_text(err))
            errors["base"] = "mfa_required"
            return self.async_show_form(step_id="user", data_schema=_user_schema(language), errors=errors)
        except EntergyAuthError as err:
            _LOGGER.debug("Entergy setup authentication failed: %s", redact_text(err))
            errors["base"] = "invalid_auth"
            return self.async_show_form(step_id="user", data_schema=_user_schema(language), errors=errors)
        except (EntergyApiError, ClientError, TimeoutError) as err:
            _LOGGER.debug("Entergy setup connection/API failed: %s", redact_text(err))
            errors["base"] = "cannot_connect"
            return self.async_show_form(step_id="user", data_schema=_user_schema(language), errors=errors)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unexpected Entergy setup error")
            _LOGGER.debug("Unexpected Entergy setup detail: %s", redact_text(err))
            errors["base"] = "unknown"
            return self.async_show_form(step_id="user", data_schema=_user_schema(language), errors=errors)

        account_options = []
        for account in accounts:
            account_identifier = _account_id(account)
            if account_identifier:
                account_options.append({"value": account_identifier, "label": _account_label(account)})

        if not account_options:
            errors["base"] = "no_accounts"
            return self.async_show_form(step_id="user", data_schema=_user_schema(language), errors=errors)

        self._flow_data = {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
            CONF_LANGUAGE: language,
            "account_options": account_options,
        }
        return await self.async_step_account()

    async def async_step_account(self, user_input: dict[str, Any] | None = None):
        """Handle account selection."""
        if not self._flow_data:
            return await self.async_step_user()

        errors: dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(
                step_id="account",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ACCOUNT_ID): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=self._flow_data["account_options"],
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        )
                    }
                ),
                errors=errors,
            )

        account_id = user_input[CONF_ACCOUNT_ID]
        username = self._flow_data[CONF_USERNAME]

        await self.async_set_unique_id(f"{username}_{account_id}")
        self._abort_if_unique_id_configured()

        data = {
            CONF_USERNAME: username,
            CONF_PASSWORD: self._flow_data[CONF_PASSWORD],
            CONF_LANGUAGE: self._flow_data[CONF_LANGUAGE],
            CONF_ACCOUNT_ID: account_id,
        }

        return self.async_create_entry(
            title=friendly_account_name(account_id),
            data=data,
            options={CONF_SCAN_INTERVAL_SECONDS: DEFAULT_SCAN_INTERVAL_SECONDS},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Start reauthentication for an existing entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Ask for the current password and keep the existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        username = entry.data[CONF_USERNAME]
        language = entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        account_id = str(entry.data[CONF_ACCOUNT_ID])

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            try:
                accounts = await _validate_and_fetch_accounts(
                    self.hass,
                    username,
                    password,
                    language,
                    extra_secrets=(account_id, username),
                )
            except EntergyMfaRequired as err:
                _LOGGER.debug("Entergy reauth requires unsupported MFA step: %s", redact_text(err))
                errors["base"] = "mfa_required"
            except EntergyAuthError as err:
                _LOGGER.debug("Entergy reauth authentication failed: %s", redact_text(err))
                errors["base"] = "invalid_auth"
            except (EntergyApiError, ClientError, TimeoutError) as err:
                _LOGGER.debug("Entergy reauth connection/API failed: %s", redact_text(err))
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Unexpected Entergy reauth error")
                _LOGGER.debug("Unexpected Entergy reauth detail: %s", redact_text(err))
                errors["base"] = "unknown"
            else:
                known_ids = {identifier for account in accounts if (identifier := _account_id(account))}
                if known_ids and account_id not in known_ids:
                    errors["base"] = "account_missing"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_PASSWORD: password},
                    )
                    if not entry.update_listeners:
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(),
            description_placeholders={"username": username},
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return options flow."""
        return EntergyMobileOptionsFlow()


class EntergyMobileOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Entergy."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage options."""
        if user_input is not None:
            interval = normalize_scan_interval_seconds(user_input.get(CONF_SCAN_INTERVAL_SECONDS))
            return self.async_create_entry(
                title="",
                data={CONF_SCAN_INTERVAL_SECONDS: interval},
            )

        current = normalize_scan_interval_seconds(
            self.config_entry.options.get(
                CONF_SCAN_INTERVAL_SECONDS,
                DEFAULT_SCAN_INTERVAL_SECONDS,
            )
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL_SECONDS,
                        default=current,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCAN_INTERVAL_SECONDS,
                            max=MAX_SCAN_INTERVAL_SECONDS,
                        ),
                    ),
                }
            ),
        )
