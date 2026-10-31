"""Data coordinator and parsing for Entergy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import inspect
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EntergyApiClient, EntergyApiError, EntergyAuthError, EntergyMfaRequired
from .const import (
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .statistics import (
    async_import_hourly_statistics,
    interval_is_provisional,
    latest_finalized_hour,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class HourlyUsage:
    """One hourly usage interval."""

    timestamp: str
    usage: float
    cost: float | None
    is_estimated: bool
    is_provisional: bool = False


def _parse_iso_z(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utcnow() -> datetime:
    now = dt_util.utcnow()
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _get_path(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _sum_usage(items: list[Any]) -> float:
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        value = _safe_float(item.get("usage"))
        if value is not None:
            total += value
    return round(total, 3)


def _sum_cost(items: list[Any]) -> float:
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        value = _safe_float(item.get("cost"))
        if value is not None:
            total += value
    return round(total, 2)


def extract_hourly_usage(payload: dict[str, Any]) -> list[HourlyUsage]:
    """Extract hourly usage records from the weeklyusage schema."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []

    daily_electric = _safe_list(_get_path(data, "daily", "electric"))
    records: list[HourlyUsage] = []

    for day in daily_electric:
        if not isinstance(day, dict):
            continue
        for hourly in _safe_list(day.get("hourly")):
            if not isinstance(hourly, dict):
                continue
            timestamp = hourly.get("date")
            usage = _safe_float(hourly.get("usage"))
            if not isinstance(timestamp, str) or usage is None:
                continue
            cost = _safe_float(hourly.get("cost"))
            is_estimated, is_provisional = interval_is_provisional(hourly)
            records.append(
                HourlyUsage(
                    timestamp=timestamp,
                    usage=usage,
                    cost=cost,
                    is_estimated=is_estimated,
                    is_provisional=is_provisional,
                )
            )

    return sorted(records, key=lambda x: x.timestamp)


def parse_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse the Entergy weeklyusage payload."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}

    monthly_electric = _safe_list(_get_path(data, "monthly", "electric"))
    weekly_electric = _safe_list(_get_path(data, "weekly", "electric"))
    hourly_records = extract_hourly_usage(payload)
    latest = latest_finalized_hour(hourly_records)

    latest_day = None
    latest_day_records: list[HourlyUsage] = []
    parsed_hours: list[tuple[HourlyUsage, datetime]] = []
    for record in hourly_records:
        try:
            parsed_hours.append((record, _parse_iso_z(record.timestamp)))
        except ValueError:
            continue
    if parsed_hours:
        latest_day = parsed_hours[-1][1].date()
        latest_day_records = [
            record for record, moment in parsed_hours if moment.date() == latest_day
        ]

    latest_month_entry = monthly_electric[-1] if monthly_electric else {}
    month_to_date_usage = None
    month_to_date_cost = None
    month_to_date_date = None
    if isinstance(latest_month_entry, dict):
        month_to_date_usage = _safe_float(latest_month_entry.get("usage"))
        month_to_date_cost = _safe_float(latest_month_entry.get("cost"))
        month_to_date_date = latest_month_entry.get("date")

    latest_import = max(latest.usage, 0.0) if latest else None
    latest_export = abs(min(latest.usage, 0.0)) if latest else None

    latest_day_net = round(sum(record.usage for record in latest_day_records), 3)
    latest_day_import = round(sum(max(record.usage, 0.0) for record in latest_day_records), 3)
    latest_day_export = round(sum(abs(min(record.usage, 0.0)) for record in latest_day_records), 3)
    latest_day_cost = round(
        sum(record.cost for record in latest_day_records if record.cost is not None),
        2,
    )

    return {
        "last_7_days_net_kwh": _sum_usage(weekly_electric),
        "last_7_days_cost": _sum_cost(weekly_electric),
        "latest_day": latest_day.isoformat() if latest_day else None,
        "latest_day_net_kwh": latest_day_net if latest_day_records else None,
        "latest_day_import_kwh": latest_day_import if latest_day_records else None,
        "latest_day_export_kwh": latest_day_export if latest_day_records else None,
        "latest_day_cost": latest_day_cost if latest_day_records else None,
        "latest_hour_timestamp": latest.start.isoformat() if latest else None,
        "latest_hour_net_kwh": round(latest.usage, 3) if latest else None,
        "latest_hour_import_kwh": round(latest_import, 3) if latest_import is not None else None,
        "latest_hour_export_kwh": round(latest_export, 3) if latest_export is not None else None,
        "latest_hour_cost": round(latest.cost, 2) if latest and latest.cost is not None else None,
        "latest_hour_estimated": latest.is_estimated if latest else None,
        "month_to_date_net_kwh": round(month_to_date_usage, 3) if month_to_date_usage is not None else None,
        "month_to_date_cost": round(month_to_date_cost, 2) if month_to_date_cost is not None else None,
        "month_to_date_date": month_to_date_date,
        "hourly_records": hourly_records,
    }


class EntergyUsageStore:
    """Persistent cumulative energy store for Energy dashboard sensors."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store[dict[str, Any]](
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}_{entry_id}",
        )
        self._data: dict[str, Any] = {}

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        self._data = loaded if isinstance(loaded, dict) else {}
        self._data.setdefault("total_import_kwh", 0.0)
        self._data.setdefault("total_export_kwh", 0.0)
        self._data.setdefault("intervals", {})

    async def async_process(self, records: list[HourlyUsage]) -> dict[str, Any]:
        intervals: dict[str, Any] = self._data.setdefault("intervals", {})
        changed = False

        for record in records:
            existing = intervals.get(record.timestamp)
            import_kwh = round(max(record.usage, 0.0), 6)
            export_kwh = round(abs(min(record.usage, 0.0)), 6)

            if existing is None:
                self._data["total_import_kwh"] = round(
                    float(self._data.get("total_import_kwh", 0.0)) + import_kwh,
                    6,
                )
                self._data["total_export_kwh"] = round(
                    float(self._data.get("total_export_kwh", 0.0)) + export_kwh,
                    6,
                )
                intervals[record.timestamp] = {
                    "usage": record.usage,
                    "import": import_kwh,
                    "export": export_kwh,
                    "cost": record.cost,
                    "isEstimated": record.is_estimated,
                }
                changed = True
                continue

            old_import = _safe_float(existing.get("import")) or 0.0
            old_export = _safe_float(existing.get("export")) or 0.0
            import_diff = max(import_kwh - old_import, 0.0)
            export_diff = max(export_kwh - old_export, 0.0)

            if import_diff > 0:
                self._data["total_import_kwh"] = round(
                    float(self._data.get("total_import_kwh", 0.0)) + import_diff,
                    6,
                )
                changed = True
            if export_diff > 0:
                self._data["total_export_kwh"] = round(
                    float(self._data.get("total_export_kwh", 0.0)) + export_diff,
                    6,
                )
                changed = True

            if existing.get("usage") != record.usage or existing.get("isEstimated") != record.is_estimated:
                existing.update(
                    {
                        "usage": record.usage,
                        "import": max(old_import, import_kwh),
                        "export": max(old_export, export_kwh),
                        "cost": record.cost,
                        "isEstimated": record.is_estimated,
                    }
                )
                changed = True

        changed = self._prune_intervals(intervals) or changed

        if changed:
            await self._store.async_save(self._data)

        return {
            "total_import_kwh": round(float(self._data.get("total_import_kwh", 0.0)), 3),
            "total_export_kwh": round(float(self._data.get("total_export_kwh", 0.0)), 3),
            "tracked_interval_count": len(intervals),
        }

    def _prune_intervals(self, intervals: dict[str, Any]) -> bool:
        cutoff = _utcnow() - timedelta(days=370)
        to_delete: list[str] = []
        for timestamp in intervals:
            try:
                if _parse_iso_z(timestamp) < cutoff:
                    to_delete.append(timestamp)
            except ValueError:
                to_delete.append(timestamp)

        for timestamp in to_delete:
            intervals.pop(timestamp, None)

        return bool(to_delete)


_COORDINATOR_ACCEPTS_CONFIG_ENTRY = "config_entry" in inspect.signature(
    DataUpdateCoordinator.__init__
).parameters
_MAX_BACKOFF_STEPS = 6


class EntergyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Entergy usage polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EntergyApiClient,
        account_id: str,
        entry_id: str,
        config_entry: ConfigEntry | None = None,
        scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
    ) -> None:
        self._configured_interval = timedelta(seconds=scan_interval_seconds)
        self._transient_failures = 0
        coordinator_kwargs: dict[str, Any] = {}
        if _COORDINATOR_ACCEPTS_CONFIG_ENTRY:
            coordinator_kwargs["config_entry"] = config_entry
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=self._configured_interval,
            **coordinator_kwargs,
        )
        self._client = client
        self._account_id = account_id
        self._usage_store = EntergyUsageStore(hass, entry_id)

    async def async_initialize(self) -> None:
        """Load persistent state."""
        await self._usage_store.async_load()

    def _stop_polling_for_auth_failure(self) -> None:
        """Stop scheduled polls so a rejected password is not retried."""
        self._transient_failures = 0
        self.update_interval = None

    def _apply_backoff(self) -> None:
        """Delay the next poll after a transient failure."""
        self._transient_failures += 1
        multiplier = 2 ** min(self._transient_failures, _MAX_BACKOFF_STEPS)
        delay = min(
            self._configured_interval.total_seconds() * multiplier,
            float(MAX_SCAN_INTERVAL_SECONDS),
        )
        self.update_interval = timedelta(seconds=delay)
        _LOGGER.debug("Scheduling next Entergy poll in %s seconds", int(delay))

    def _reset_backoff(self) -> None:
        """Restore the configured poll interval after a successful update."""
        self._transient_failures = 0
        if self.update_interval != self._configured_interval:
            self.update_interval = self._configured_interval

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            payload = await self._client.async_fetch_current_usage(self._account_id)
        except EntergyMfaRequired as err:
            self._stop_polling_for_auth_failure()
            raise ConfigEntryAuthFailed(str(err)) from err
        except EntergyAuthError as err:
            self._stop_polling_for_auth_failure()
            raise ConfigEntryAuthFailed(str(err)) from err
        except EntergyApiError as err:
            self._apply_backoff()
            raise UpdateFailed(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Entergy update network error: %s", self._client.redact(err))
            self._apply_backoff()
            raise UpdateFailed("Could not reach Entergy") from None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Unexpected Entergy update error: %s", self._client.redact(err))
            self._apply_backoff()
            raise UpdateFailed("Unexpected error updating Entergy data") from None

        try:
            parsed = parse_usage_payload(payload)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Entergy payload parse error: %s", self._client.redact(err))
            self._apply_backoff()
            raise UpdateFailed("Unexpected Entergy usage payload") from None

        if not parsed:
            self._apply_backoff()
            raise UpdateFailed("Unexpected Entergy usage payload")

        hourly_records = parsed.pop("hourly_records", [])
        await async_import_hourly_statistics(
            self.hass,
            self._account_id,
            hourly_records,
            currency=self.hass.config.currency,
        )
        totals = await self._usage_store.async_process(hourly_records)
        parsed.update(totals)
        parsed["last_update"] = _utcnow().isoformat()
        self._reset_backoff()
        return parsed
