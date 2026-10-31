"""External hourly statistics for Entergy grid import.

Entergy finalizes ``weeklyusage`` hours in blocks, several hours behind real
time. Hours that are not finalized are reported as 0. Writing those totals
through a ``total_increasing`` sensor files the whole block in the hour the
poll ran. These helpers import each finalized hour at its own start instead.

Each poll rewrites a rolling window. The running ``sum`` is rebuilt from the
last stored sum strictly before that window, so a correction replaces earlier
rows instead of adding them again.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
    valid_statistic_id,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from .const import ACCOUNT_TIME_ZONE, DOMAIN, STATISTICS_WINDOW_DAYS
from .redact import redact_text

_LOGGER = logging.getLogger(__name__)

_CHICAGO = ZoneInfo(ACCOUNT_TIME_ZONE)
_LONG_LOOKBACK = timedelta(days=400)

_TRUE_STRINGS = {"1", "true", "yes", "y"}
_PROVISIONAL_WORDS = {
    "estimated",
    "estimate",
    "provisional",
    "preliminary",
    "forecast",
    "projected",
    "incomplete",
    "pending",
}
_FLAG_KEYS = (
    "estimated",
    "isProvisional",
    "is_provisional",
    "provisional",
    "isPreliminary",
    "is_preliminary",
    "preliminary",
    "isForecast",
    "is_forecast",
    "forecast",
)
_QUALITY_KEYS = (
    "quality",
    "readingQuality",
    "reading_quality",
    "valueType",
    "value_type",
    "readingType",
    "reading_type",
)


@dataclass(frozen=True, slots=True)
class ParsedHour:
    """One hourly interval aligned to a UTC hour start."""

    start: datetime
    usage: float
    cost: float | None
    is_estimated: bool
    is_provisional: bool


def account_id_slug(account_id: str) -> str:
    """Return a statistic-id object id that does not leak separators or case."""
    slug = []
    previous_underscore = False
    for char in str(account_id).strip().lower():
        if "a" <= char <= "z" or "0" <= char <= "9":
            slug.append(char)
            previous_underscore = False
            continue
        if previous_underscore:
            continue
        slug.append("_")
        previous_underscore = True
    text = "".join(slug).strip("_")
    return text or "account"


def account_ending(account_id: str) -> str:
    """Return the last four characters of an account, for display only."""
    text = str(account_id).strip()
    if len(text) < 4:
        return "account"
    return text[-4:]


def grid_import_statistic_id(account_id: str) -> str:
    """Return the external statistic id for grid import energy."""
    return f"{DOMAIN}:grid_import_{account_id_slug(account_id)}"


def grid_import_cost_statistic_id(account_id: str) -> str:
    """Return the external statistic id for grid import cost."""
    return f"{DOMAIN}:grid_import_cost_{account_id_slug(account_id)}"


def energy_statistic_metadata(account_id: str) -> StatisticMetaData:
    """Return metadata for the grid import energy statistic."""
    return {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"Entergy grid import ending {account_ending(account_id)}",
        "source": DOMAIN,
        "statistic_id": grid_import_statistic_id(account_id),
        "unit_class": EnergyConverter.UNIT_CLASS,
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }


def cost_statistic_metadata(account_id: str, currency: str) -> StatisticMetaData:
    """Return metadata for the grid import cost statistic."""
    return {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"Entergy grid import cost ending {account_ending(account_id)}",
        "source": DOMAIN,
        "statistic_id": grid_import_cost_statistic_id(account_id),
        "unit_class": None,
        "unit_of_measurement": currency,
    }


def _flag_is_true(value: Any) -> bool:
    """Return whether a payload flag marks an interval as not finalized."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        return text in _TRUE_STRINGS or text in _PROVISIONAL_WORDS
    return False


def interval_is_provisional(item: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return ``(is_estimated, is_provisional)`` for one hourly payload object.

    The fixture schema uses ``isEstimated``. Other names are honored when the
    service sends them. A missing flag is not provisional.
    """
    estimated = _flag_is_true(item.get("isEstimated", item.get("is_estimated", False)))
    provisional = estimated
    for key in _FLAG_KEYS:
        if key in item and _flag_is_true(item.get(key)):
            provisional = True
    for key in _QUALITY_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip().lower() in _PROVISIONAL_WORDS:
            provisional = True
    return estimated, provisional


def _wall_clock(moment: datetime) -> tuple[int, int, int, int, int, int]:
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)


def parse_interval_start(value: str, seen_naive: set[datetime] | None = None) -> datetime | None:
    """Return the UTC hour start for an Entergy hourly timestamp.

    Timezone-aware values, including the ``Z`` timestamps in the fixtures, are
    converted to UTC. Naive values are interpreted as America/Chicago wall
    time. A repeated naive timestamp is the second occurrence of a fall-back
    hour. A spring-forward gap is skipped. The result is always timezone-aware
    and aligned to the top of the UTC hour.
    """
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        _LOGGER.debug("Skipping an hourly interval with an unreadable timestamp")
        return None

    if parsed.tzinfo is None:
        fold = 0
        naive = parsed.replace(fold=0)
        if seen_naive is not None and naive in seen_naive:
            fold = 1
        elif seen_naive is not None:
            seen_naive.add(naive)
        local = naive.replace(tzinfo=_CHICAGO, fold=fold)
        try:
            utc = local.astimezone(UTC)
            roundtrip = utc.astimezone(_CHICAGO)
        except (OverflowError, OSError, ValueError):
            _LOGGER.debug("Skipping an hourly interval with a timestamp outside the supported range")
            return None
        if _wall_clock(roundtrip) != _wall_clock(naive):
            _LOGGER.debug("Skipping an hourly interval that falls in a daylight-saving gap")
            return None
        moment = utc
    else:
        moment = parsed.astimezone(UTC)

    return moment.replace(minute=0, second=0, microsecond=0)


def _parse_hours(records: Sequence[Any]) -> list[ParsedHour]:
    """Parse, align, and collapse hourly records to one row per UTC hour."""
    seen_naive: set[datetime] = set()
    by_start: dict[datetime, ParsedHour] = {}
    for record in records:
        start = parse_interval_start(record.timestamp, seen_naive)
        if start is None:
            continue
        # A repeated timestamp is a correction. Keep the later payload row.
        by_start[start] = ParsedHour(
            start=start,
            usage=record.usage,
            cost=record.cost,
            is_estimated=record.is_estimated,
            is_provisional=record.is_provisional,
        )
    return [by_start[start] for start in sorted(by_start)]


def _select_finalized(hours: Sequence[ParsedHour]) -> list[ParsedHour]:
    """Drop provisional hours and trailing zeros that are not finalized yet.

    A zero hour that is followed by a later finalized reading is kept. Zeros
    after the last nonzero finalized hour are the block Entergy has not filled
    in yet, so they are left out until a later poll.
    """
    last_nonzero = -1
    for index, hour in enumerate(hours):
        if hour.is_provisional or hour.usage == 0:
            continue
        last_nonzero = index
    if last_nonzero < 0:
        return []
    return [
        hour
        for index, hour in enumerate(hours)
        if index <= last_nonzero and not hour.is_provisional
    ]


def latest_finalized_hour(records: Sequence[Any]) -> ParsedHour | None:
    """Return the newest finalized hour in the payload."""
    finalized = _select_finalized(_parse_hours(records))
    if not finalized:
        return None
    return finalized[-1]


def statistics_import_hours(
    records: Sequence[Any],
    *,
    window_days: int = STATISTICS_WINDOW_DAYS,
) -> list[ParsedHour]:
    """Return the finalized hours inside the rolling import window.

    The window is the last ``window_days`` of whatever this payload returned,
    measured back from its newest parsed timestamp. Finalized filtering is
    applied after the cut so a trailing zero does not hide the hours before it.
    """
    parsed = _parse_hours(records)
    if not parsed:
        return []
    cutoff = parsed[-1].start - timedelta(days=window_days)
    windowed = [hour for hour in parsed if hour.start >= cutoff]
    return _select_finalized(windowed)


def _energy_delta(hour: ParsedHour) -> float:
    return round(max(hour.usage, 0.0), 6)


def _cost_delta(hour: ParsedHour) -> float:
    if hour.cost is None:
        return 0.0
    return round(hour.cost, 6)


def build_running_sum(
    hours: Sequence[ParsedHour],
    base_sum: float,
    value_for: Callable[[ParsedHour], float],
) -> list[StatisticData]:
    """Build statistic rows whose ``sum`` continues from ``base_sum``.

    ``base_sum`` must be the stored sum from before the first hour. Rebuilding
    the window from that point overwrites corrected hours without counting them
    twice.
    """
    running = round(float(base_sum), 6)
    points: list[StatisticData] = []
    for hour in hours:
        delta = round(float(value_for(hour)), 6)
        running = round(running + delta, 6)
        points.append({"start": hour.start, "state": delta, "sum": running})
    return points


def _row_start(row: Mapping[str, Any]) -> datetime | None:
    start = row.get("start")
    if isinstance(start, datetime):
        if start.tzinfo is None:
            return start.replace(tzinfo=UTC)
        return start.astimezone(UTC)
    if isinstance(start, (int, float)):
        return datetime.fromtimestamp(float(start), UTC)
    return None


def _row_sum(row: Mapping[str, Any]) -> float:
    value = row.get("sum")
    if value is None:
        return 0.0
    return round(float(value), 6)


def last_sum_before(hass: HomeAssistant, statistic_id: str, before: datetime) -> float:
    """Return the newest stored sum whose start is strictly before ``before``.

    This reads the recorder database and must run in the recorder executor.
    """
    # The recorder may discard members of this set, so do not reuse it.
    latest = get_last_statistics(hass, 1, statistic_id, True, {"sum"})
    rows = latest.get(statistic_id) or []
    if not rows:
        return 0.0
    latest_start = _row_start(rows[0])
    if latest_start is not None and latest_start < before:
        return _row_sum(rows[0])

    short_lookback = timedelta(days=STATISTICS_WINDOW_DAYS + 2)
    for lookback in (short_lookback, _LONG_LOOKBACK):
        period = statistics_during_period(
            hass,
            before - lookback,
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        period_rows = period.get(statistic_id) or []
        if period_rows:
            return _row_sum(period_rows[-1])
    return 0.0


def _has_cost(hours: Sequence[ParsedHour]) -> bool:
    return any(hour.cost is not None for hour in hours)


async def async_import_hourly_statistics(
    hass: HomeAssistant,
    account_id: str,
    records: Sequence[Any],
    currency: str | None,
) -> None:
    """Import the rolling finalized window as external statistics.

    Failures are logged without account numbers, tokens, or payload bodies.
    Sensor updates still proceed when the recorder cannot store statistics.
    """
    try:
        recorder = get_instance(hass)
    except KeyError:
        _LOGGER.debug("Recorder is not available; Entergy hourly statistics were not imported")
        return

    try:
        await _async_import_with_recorder(hass, recorder, account_id, records, currency)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Entergy hourly statistics could not be imported")
        _LOGGER.debug(
            "Entergy statistics import error: %s",
            redact_text(err, (str(account_id),)),
        )


async def _async_import_with_recorder(
    hass: HomeAssistant,
    recorder: Any,
    account_id: str,
    records: Sequence[Any],
    currency: str | None,
) -> None:
    energy_id = grid_import_statistic_id(account_id)
    if not valid_statistic_id(energy_id):
        _LOGGER.debug("Skipping Entergy statistics import because the statistic id is not valid")
        return

    hours = statistics_import_hours(records)
    if not hours:
        _LOGGER.debug(
            "No finalized Entergy hours to import for the account ending %s",
            account_ending(account_id),
        )
        return

    window_start = hours[0].start
    currency_code = currency.strip() if isinstance(currency, str) else ""
    import_cost = bool(currency_code) and _has_cost(hours)
    cost_id = grid_import_cost_statistic_id(account_id) if import_cost else None
    if cost_id is not None and not valid_statistic_id(cost_id):
        cost_id = None

    energy_base, cost_base = await recorder.async_add_executor_job(
        _read_bases,
        hass,
        energy_id,
        cost_id,
        window_start,
    )

    energy_points = build_running_sum(hours, energy_base, _energy_delta)
    async_add_external_statistics(hass, energy_statistic_metadata(account_id), energy_points)
    _LOGGER.debug(
        "Imported %s Entergy hourly grid statistics for the account ending %s",
        len(energy_points),
        account_ending(account_id),
    )

    if cost_id is None:
        return

    cost_points = build_running_sum(hours, cost_base, _cost_delta)
    async_add_external_statistics(
        hass,
        cost_statistic_metadata(account_id, currency_code),
        cost_points,
    )
    _LOGGER.debug(
        "Imported %s Entergy hourly cost statistics for the account ending %s",
        len(cost_points),
        account_ending(account_id),
    )


def _read_bases(
    hass: HomeAssistant,
    energy_id: str,
    cost_id: str | None,
    window_start: datetime,
) -> tuple[float, float]:
    """Read energy and cost bases. Runs in the recorder executor."""
    energy_base = last_sum_before(hass, energy_id, window_start)
    cost_base = last_sum_before(hass, cost_id, window_start) if cost_id else 0.0
    return energy_base, cost_base
