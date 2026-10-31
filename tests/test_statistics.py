"""Hourly external statistics for Entergy grid import."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import EnergyConverter

from custom_components.entergy_mobile.const import DOMAIN, STATISTICS_WINDOW_DAYS
from custom_components.entergy_mobile.coordinator import extract_hourly_usage, parse_usage_payload
from custom_components.entergy_mobile.statistics import (
    account_id_slug,
    cost_statistic_metadata,
    energy_statistic_metadata,
    grid_import_cost_statistic_id,
    grid_import_statistic_id,
    latest_finalized_hour,
    statistics_import_hours,
)

from tests.common import ACCOUNT_ID, assert_no_secrets, async_setup_entry, make_entry


def _payload(hours: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "daily": {"electric": [{"hourly": hours}]},
            "weekly": {"electric": [{"usage": 1, "cost": 0.2}]},
            "monthly": {"electric": [{"usage": 1, "cost": 0.2, "date": "2026-10-01"}]},
        }
    }


def _hour(
    when: str,
    usage: float,
    cost: float | None = None,
    **flags: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {"date": when, "usage": usage}
    if cost is not None:
        item["cost"] = cost
    item.update(flags)
    return item


def _records(hours: list[dict[str, Any]]):
    return extract_hourly_usage(_payload(hours))


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


class StatStore:
    """In-memory stand-in for the recorder statistic tables."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[float, dict[str, float]]] = {}
        self.calls: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        self.executor_calls = 0
        self.in_executor = False

    def get_instance(self, hass: Any) -> StatStore:
        return self

    async def async_add_executor_job(self, func, *args):
        self.executor_calls += 1
        self.in_executor = True
        try:
            return func(*args)
        finally:
            self.in_executor = False

    def get_last_statistics(self, hass, number, statistic_id, convert, types):
        assert self.in_executor
        rows = sorted(self.rows.get(statistic_id, {}).values(), key=lambda row: row["start"], reverse=True)
        picked = rows[:number]
        return {statistic_id: picked} if picked else {}

    def statistics_during_period(self, hass, start, end, statistic_ids, period, units, types):
        assert self.in_executor
        assert period == "hour"
        start_ts = start.timestamp()
        end_ts = None if end is None else end.timestamp()
        result = {}
        for statistic_id in statistic_ids or []:
            matched = []
            for row in sorted(self.rows.get(statistic_id, {}).values(), key=lambda item: item["start"]):
                if row["start"] >= start_ts and (end_ts is None or row["start"] < end_ts):
                    matched.append(row)
            if matched:
                result[statistic_id] = matched
        return result

    def async_add_external_statistics(self, hass, metadata, statistics) -> None:
        assert not self.in_executor
        points = [dict(point) for point in statistics]
        self.calls.append((dict(metadata), points))
        bucket = self.rows.setdefault(metadata["statistic_id"], {})
        for point in points:
            bucket[point["start"].timestamp()] = {
                "start": point["start"].timestamp(),
                "sum": point["sum"],
                "state": point["state"],
            }

    def sums(self, statistic_id: str) -> dict[datetime, float]:
        return {
            datetime.fromtimestamp(start, UTC): row["sum"]
            for start, row in self.rows.get(statistic_id, {}).items()
        }


def _patch_store(monkeypatch) -> StatStore:
    store = StatStore()
    monkeypatch.setattr(
        "custom_components.entergy_mobile.statistics.get_instance",
        store.get_instance,
    )
    monkeypatch.setattr(
        "custom_components.entergy_mobile.statistics.get_last_statistics",
        store.get_last_statistics,
    )
    monkeypatch.setattr(
        "custom_components.entergy_mobile.statistics.statistics_during_period",
        store.statistics_during_period,
    )
    monkeypatch.setattr(
        "custom_components.entergy_mobile.statistics.async_add_external_statistics",
        store.async_add_external_statistics,
    )
    return store


def _points(store: StatStore, statistic_id: str) -> list[dict[str, Any]]:
    matched = [points for metadata, points in store.calls if metadata["statistic_id"] == statistic_id]
    assert matched
    return matched[-1]


def test_statistic_metadata_uses_sum_only_energy() -> None:
    """Grid import metadata matches the Home Assistant 2026 statistics contract."""
    assert STATISTICS_WINDOW_DAYS == 4
    statistic_id = grid_import_statistic_id(ACCOUNT_ID)
    cost_id = grid_import_cost_statistic_id(ACCOUNT_ID)
    assert statistic_id == "entergy_mobile:grid_import_100200300400"
    assert cost_id == "entergy_mobile:grid_import_cost_100200300400"
    assert valid_statistic_id(statistic_id)
    assert valid_statistic_id(cost_id)
    assert account_id_slug("AB-12 34") == "ab_12_34"
    assert valid_statistic_id(grid_import_statistic_id("AB-12 34"))

    energy = energy_statistic_metadata(ACCOUNT_ID)
    assert energy["source"] == DOMAIN
    assert energy["statistic_id"] == statistic_id
    assert energy["name"] == "Entergy grid import ending 0400"
    assert energy["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR
    assert energy["unit_class"] == EnergyConverter.UNIT_CLASS == "energy"
    assert energy["has_sum"] is True
    assert energy["has_mean"] is False
    assert energy["mean_type"] is StatisticMeanType.NONE
    assert ACCOUNT_ID not in energy["name"]

    cost = cost_statistic_metadata(ACCOUNT_ID, "USD")
    assert cost["statistic_id"] == cost_id
    assert cost["name"] == "Entergy grid import cost ending 0400"
    assert cost["unit_of_measurement"] == "USD"
    assert cost["unit_class"] is None
    assert cost["has_sum"] is True
    assert cost["has_mean"] is False
    assert cost["mean_type"] is StatisticMeanType.NONE
    assert cost["source"] == DOMAIN


def test_finalized_hours_keep_interior_zeros_and_drop_the_tail() -> None:
    """Trailing zeros and provisional hours are skipped. Interior zeros stay."""
    records = _records(
        [
            _hour("2026-10-24T15:00:00Z", 1.0, isEstimated=False),
            _hour("2026-10-24T16:00:00Z", 0.0, isEstimated=False),
            _hour("2026-10-24T17:00:00Z", 4.0, isEstimated=True),
            _hour("2026-10-24T18:00:00Z", 2.0, isEstimated="false"),
            _hour("2026-10-24T19:00:00Z", 3.0, isEstimated="true"),
            _hour("2026-10-24T20:00:00Z", 0.0, isEstimated=False),
            _hour("2026-10-24T21:00:00Z", 0.0, quality="estimated"),
        ]
    )
    imported = statistics_import_hours(records)
    assert [hour.start for hour in imported] == [
        _utc(2026, 10, 24, 15),
        _utc(2026, 10, 24, 16),
        _utc(2026, 10, 24, 18),
    ]
    assert [hour.usage for hour in imported] == [1.0, 0.0, 2.0]

    latest = latest_finalized_hour(records)
    assert latest is not None
    assert latest.start == _utc(2026, 10, 24, 18)
    assert latest.usage == 2.0

    flagged = _records(
        [
            _hour("2026-10-24T15:00:00Z", 1.5, quality="actual"),
            _hour("2026-10-24T16:00:00Z", 9.0, quality="Estimated"),
            _hour("2026-10-24T17:00:00Z", 0.5, isProvisional=False),
        ]
    )
    assert [hour.usage for hour in statistics_import_hours(flagged)] == [1.5, 0.5]

    repeated = _records(
        [
            _hour("2026-10-24T15:00:00Z", 1.0),
            _hour("2026-10-24T15:00:00Z", 5.0),
        ]
    )
    once = statistics_import_hours(repeated)
    assert len(once) == 1
    assert once[0].usage == 5.0


def test_dst_days_keep_both_fall_back_hours_and_skip_the_spring_gap() -> None:
    """America/Chicago transitions produce UTC hour starts and do not raise."""
    fall_back = statistics_import_hours(
        _records(
            [
                _hour("2026-11-01T01:00:00", 1.0),
                _hour("2026-11-01T01:00:00", 2.0),
            ]
        )
    )
    assert [hour.start for hour in fall_back] == [
        _utc(2026, 11, 1, 6),
        _utc(2026, 11, 1, 7),
    ]
    assert [hour.usage for hour in fall_back] == [1.0, 2.0]

    offset = statistics_import_hours(
        _records(
            [
                _hour("2026-11-01T01:00:00-05:00", 1.0),
                _hour("2026-11-01T01:00:00-06:00", 2.0),
            ]
        )
    )
    assert [hour.start for hour in offset] == [hour.start for hour in fall_back]
    assert [hour.start.minute for hour in offset] == [0, 0]
    assert all(hour.start.tzinfo is not None for hour in offset)

    zulu = statistics_import_hours(
        _records(
            [
                _hour("2026-11-01T05:00:00Z", 1),
                _hour("2026-11-01T06:00:00Z", 1),
                _hour("2026-11-01T07:00:00Z", 1),
                _hour("2026-11-01T08:00:00Z", 1),
            ]
        )
    )
    assert [hour.start for hour in zulu] == [
        _utc(2026, 11, 1, 5),
        _utc(2026, 11, 1, 6),
        _utc(2026, 11, 1, 7),
        _utc(2026, 11, 1, 8),
    ]

    spring = statistics_import_hours(
        _records(
            [
                _hour("2026-03-08T01:00:00", 1.0),
                _hour("2026-03-08T02:00:00", 9.0),
                _hour("not-a-timestamp", 8.0),
                _hour("2026-03-08T03:00:00", 2.0),
            ]
        )
    )
    assert [hour.start for hour in spring] == [
        _utc(2026, 3, 8, 7),
        _utc(2026, 3, 8, 8),
    ]
    assert [hour.usage for hour in spring] == [1.0, 2.0]
    parsed = parse_usage_payload(
        _payload(
            [
                _hour("2026-03-08T01:00:00", 1.0),
                _hour("2026-03-08T02:00:00", 9.0),
                _hour("2026-03-08T03:00:00", 2.0),
                _hour("2026-03-08T04:00:00", 0.0),
            ]
        )
    )
    assert parsed["latest_hour_net_kwh"] == 2.0
    assert parsed["latest_hour_timestamp"] == _utc(2026, 3, 8, 8).isoformat()


def test_import_window_is_the_last_four_days_of_the_payload() -> None:
    """Hours older than four days, and trailing zeros, are not in the import set."""
    hours = [
        _hour("2026-10-20T15:00:00Z", 7.0),
        _hour("2026-10-21T15:00:00Z", 1.0),
        _hour("2026-10-24T15:00:00Z", 1.0),
        _hour("2026-10-24T16:00:00Z", 0.0),
        _hour("2026-10-24T17:00:00Z", 0.0),
    ]
    imported = statistics_import_hours(_records(hours))
    assert [hour.start for hour in imported] == [
        _utc(2026, 10, 21, 15),
        _utc(2026, 10, 24, 15),
    ]
    assert imported[0].start >= _utc(2026, 10, 24, 17) - timedelta(days=STATISTICS_WINDOW_DAYS)


async def test_reimport_corrects_hours_without_double_counting(hass, entergy_api, monkeypatch, caplog) -> None:
    """A second poll rewrites the window from the sum stored before it."""
    caplog.set_level(logging.DEBUG, logger="custom_components.entergy_mobile")
    hass.config.currency = "USD"
    store = _patch_store(monkeypatch)
    energy_id = grid_import_statistic_id(ACCOUNT_ID)
    cost_id = grid_import_cost_statistic_id(ACCOUNT_ID)

    entergy_api.usage_body = _payload(
        [
            _hour("2026-10-16T15:00:00Z", 10, 1.0),
            _hour("2026-10-18T15:00:00Z", 2, 0.5),
        ]
    )
    entry = await async_setup_entry(hass, make_entry())
    assert store.executor_calls == 1
    assert _points(store, energy_id) == [
        {"start": _utc(2026, 10, 16, 15), "state": 10.0, "sum": 10.0},
        {"start": _utc(2026, 10, 18, 15), "state": 2.0, "sum": 12.0},
    ]
    assert [point["sum"] for point in _points(store, cost_id)] == [1.0, 1.5]

    entergy_api.usage_body = _payload(
        [
            _hour("2026-10-16T15:00:00Z", 10, 1.0),
            _hour("2026-10-18T15:00:00Z", 5, 0.5),
            _hour("2026-10-20T15:00:00Z", 1, 0.25),
            _hour("2026-10-21T15:00:00Z", 0, 0),
            _hour("2026-10-22T15:00:00Z", 1, 0.25),
        ]
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_refresh()

    energy = _points(store, energy_id)
    assert [(point["start"], point["state"], point["sum"]) for point in energy] == [
        (_utc(2026, 10, 18, 15), 5.0, 15.0),
        (_utc(2026, 10, 20, 15), 1.0, 16.0),
        (_utc(2026, 10, 21, 15), 0.0, 16.0),
        (_utc(2026, 10, 22, 15), 1.0, 17.0),
    ]
    # The October 16 sum stays where the first poll left it.
    assert store.sums(energy_id)[_utc(2026, 10, 16, 15)] == 10.0
    window_usage = sum(point["state"] for point in energy)
    assert energy[-1]["sum"] == 17.0
    assert energy[-1]["sum"] != 12.0 + window_usage

    cost = _points(store, cost_id)
    assert [point["sum"] for point in cost] == [1.5, 1.75, 1.75, 2.0]
    assert cost[-1]["sum"] != 1.5 + sum(point["state"] for point in cost)

    energy_meta = next(metadata for metadata, _points in store.calls if metadata["statistic_id"] == energy_id)
    assert energy_meta["name"] == "Entergy grid import ending 0400"
    assert energy_meta["has_mean"] is False
    assert energy_meta["mean_type"] is StatisticMeanType.NONE
    assert energy_meta["unit_class"] == "energy"
    cost_meta = next(metadata for metadata, _points in store.calls if metadata["statistic_id"] == cost_id)
    assert cost_meta["unit_of_measurement"] == "USD"
    assert cost_meta["unit_class"] is None
    assert_no_secrets(caplog.text)
    assert ACCOUNT_ID not in caplog.text


async def test_cost_statistics_are_skipped_when_the_payload_has_no_cost(hass, entergy_api, monkeypatch) -> None:
    """Missing hourly cost does not create a cost statistic or fail the update."""
    hass.config.currency = "USD"
    store = _patch_store(monkeypatch)
    entergy_api.usage_body = _payload(
        [
            _hour("2026-10-24T15:00:00Z", 1.25),
            _hour("2026-10-24T16:00:00Z", 0.5),
        ]
    )
    entry = await async_setup_entry(hass, make_entry())
    statistic_ids = {metadata["statistic_id"] for metadata, _points in store.calls}
    assert statistic_ids == {grid_import_statistic_id(ACCOUNT_ID)}
    assert hass.data[DOMAIN][entry.entry_id]["coordinator"].last_update_success is True

    entergy_api.usage_body = _payload(
        [
            _hour("2026-10-24T15:00:00Z", 1.25, 0.4),
            _hour("2026-10-24T16:00:00Z", 0.5),
        ]
    )
    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    cost = _points(store, grid_import_cost_statistic_id(ACCOUNT_ID))
    assert [(point["state"], point["sum"]) for point in cost] == [(0.4, 0.4), (0.0, 0.4)]


async def test_latest_finalized_hour_sensors_keep_their_unique_ids(hass, entergy_api, monkeypatch) -> None:
    """Latest-hour entities follow the last finalized hour and keep unique ids."""
    store = _patch_store(monkeypatch)
    entergy_api.usage_body = _payload(
        [
            _hour("2026-10-29T14:00:00Z", -1.5, 0.0),
            _hour("2026-10-29T15:00:00Z", 1.5, 0.4, isEstimated=False),
            _hour("2026-10-29T16:00:00Z", 0, 0, isEstimated=False),
            _hour("2026-10-29T17:00:00Z", 0, 0, isEstimated=False),
        ]
    )
    entry = await async_setup_entry(hass, make_entry())
    registry = er.async_get(hass)

    def entity_for(key: str):
        unique_id = f"{DOMAIN}_{entry.entry_id}_{ACCOUNT_ID}_{key}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id is not None
        registry_entry = registry.async_get(entity_id)
        assert registry_entry is not None
        assert registry_entry.unique_id == unique_id
        state = hass.states.get(entity_id)
        assert state is not None
        return registry_entry, state

    imported, imported_state = entity_for("total_import_kwh")
    assert imported.original_name == "Imported Energy Total"
    assert imported_state.state == "1.5"

    latest, latest_state = entity_for("latest_hour_import_kwh")
    assert latest.original_name == "Latest Finalized Hour Import"
    assert latest.unique_id.endswith("latest_hour_import_kwh")
    assert latest_state.state == "1.5"
    assert latest_state.attributes["hour_start"] == "2026-10-29T15:00:00+00:00"
    assert latest_state.attributes["last_interval"] == "2026-10-29T15:00:00+00:00"

    net, net_state = entity_for("latest_hour_net_kwh")
    assert net.original_name == "Latest Finalized Hour Net Usage"
    assert net_state.state == "1.5"

    started, started_state = entity_for("latest_hour_timestamp")
    assert started.original_name == "Latest Finalized Hour Start"
    assert started_state.state.startswith("2026-10-29T15:00:00")

    points = _points(store, grid_import_statistic_id(ACCOUNT_ID))
    assert [point["start"] for point in points] == [
        _utc(2026, 10, 29, 14),
        _utc(2026, 10, 29, 15),
    ]
    assert [point["state"] for point in points] == [0.0, 1.5]
    assert [point["sum"] for point in points] == [0.0, 1.5]
