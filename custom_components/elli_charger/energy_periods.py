"""Shared helpers for summarizing Elli's historical charging *records*.

Used by both the on-demand monthly PDF report (`report.py`) and the
always-on "energy this month / last month / this year" + "last 10 charges"
sensors (`sensor.py`, computed by the coordinator in `__init__.py`), so the
month-boundary math, pagination and card-labelling logic only lives in one
place.
"""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.util import dt as dt_util

from .const import REPORT_MAX_RECORD_PAGES, REPORT_RECORDS_PAGE_SIZE

_LOGGER = logging.getLogger(__name__)

_FMT = "%Y-%m-%dT%H:%M:%SZ"
UNKNOWN_CARD_LABEL = "Ohne Karte / App"


def local_bounds_to_utc(start_local: datetime, end_local: datetime) -> tuple[str, str]:
    """Convert a [start, end) local-time window to UTC ISO8601 strings."""
    return (
        dt_util.as_utc(start_local).strftime(_FMT),
        dt_util.as_utc(end_local).strftime(_FMT),
    )


def year_bounds_utc(year: int) -> tuple[str, str]:
    """Return (created_after, created_before) UTC bounds for a full local year."""
    local_tz = dt_util.DEFAULT_TIME_ZONE
    return local_bounds_to_utc(
        datetime(year, 1, 1, tzinfo=local_tz),
        datetime(year + 1, 1, 1, tzinfo=local_tz),
    )


def month_bounds_utc(year: int, month: int) -> tuple[str, str]:
    """Return (created_after, created_before) UTC bounds for a full local month."""
    local_tz = dt_util.DEFAULT_TIME_ZONE
    start_local = datetime(year, month, 1, tzinfo=local_tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=local_tz)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=local_tz)
    return local_bounds_to_utc(start_local, end_local)


def _records_page_lenient(
    client, station_id: str, created_after: str, created_before: str, limit: int, offset: int
) -> list:
    """One page of charging records, tolerant of incomplete entries.

    v1.6.3: Elli occasionally returns records without `total_energy_wh`
    (e.g. aborted sessions). `elli-client` validates the whole page strictly,
    so a single such record made it reject the entire page - and with it the
    whole year of history. Here every record is validated on its own;
    records without an energy value are skipped (they carry no charged
    energy), other incomplete ones are kept with neutral defaults.
    Falls back to the client's own method if its internals look different.
    """
    from elli_client import ChargingRecord  # pylint: disable=import-outside-toplevel

    http = getattr(client, "client", None)
    headers = getattr(client, "_get_headers", None)
    base = getattr(client, "api_base_url", None)
    if http is None or headers is None or base is None:
        return client.get_charging_records(
            station_id=station_id,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
        )

    response = http.get(
        f"{base}/chargeathome/v1/chargingrecords",
        headers=headers(),
        params={
            "station_id": station_id,
            "limit": limit,
            "offset": offset,
            "sort": "created_at",
            "created_after": created_after,
            "created_before": created_before,
        },
    )
    if response.status_code != 200:
        raise ValueError(
            f"Failed to get charging records: {response.status_code} - {response.text}"
        )
    raw_records = (response.json() or {}).get("charging_records") or []

    records: list = []
    skipped = 0
    for raw in raw_records:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        if raw.get("total_energy_wh") is None:
            skipped += 1
            continue
        try:
            records.append(ChargingRecord(**raw))
        except Exception:  # pylint: disable=broad-except
            # Keep the record with neutral defaults for whatever is missing;
            # only energy, timestamps and card are used by this integration.
            defaults = {
                "id": "",
                "created_at": raw.get("start_date_time") or "",
                "start_date_time": raw.get("created_at") or "",
                "stop_date_time": raw.get("start_date_time") or raw.get("created_at") or "",
                "session_faulted": False,
                "charging_session_id": "",
                "transaction_id": "",
                "connector_id": 0,
                "station_id": station_id,
                "station_serial_number": "",
                "station_name": "",
                "station_model": "",
            }
            data = {**defaults, **{k: v for k, v in raw.items() if v is not None}}
            try:
                data["total_energy_wh"] = int(float(data["total_energy_wh"]))
            except (TypeError, ValueError):
                skipped += 1
                continue
            data.pop("location", None)
            records.append(ChargingRecord.model_construct(**data))
    if skipped:
        _LOGGER.debug(
            "Skipped %s Elli charging record(s) without energy value (%s - %s)",
            skipped,
            created_after,
            created_before,
        )
    # Page size is decided on the raw count, so skipping doesn't stop paging early.
    records_page_size = len(raw_records)
    return records, records_page_size


def fetch_station_records(
    client, station_id: str, created_after: str, created_before: str
) -> list:
    """Fetch every page of historical records for one station/date range.

    Runs in the executor - performs blocking HTTP requests. Capped at
    REPORT_MAX_RECORD_PAGES pages so a runaway API response can't hang
    forever or blow through Elli's rate limits.
    """
    records: list = []
    offset = 0
    for _ in range(REPORT_MAX_RECORD_PAGES):
        result = _records_page_lenient(
            client,
            station_id,
            created_after,
            created_before,
            REPORT_RECORDS_PAGE_SIZE,
            offset,
        )
        if isinstance(result, tuple):
            page, raw_count = result
        else:
            page, raw_count = result, len(result)
        records.extend(page)
        if raw_count < REPORT_RECORDS_PAGE_SIZE:
            break
        offset += REPORT_RECORDS_PAGE_SIZE
    return records


def sum_energy_kwh(records) -> float:
    """Sum total_energy_wh across a list of ChargingRecord objects, in kWh."""
    return sum((getattr(r, "total_energy_wh", 0) or 0) for r in records) / 1000


def card_label(record) -> str:
    """Best-effort human label for the card/method used to authenticate."""
    if getattr(record, "rfid_card_serial_number", None):
        return f"RFID {record.rfid_card_serial_number}"
    if getattr(record, "authentication_method", None):
        return record.authentication_method
    return UNKNOWN_CARD_LABEL


def group_records_by_month(records, solar_fn=None) -> dict[str, dict]:
    """Aggregate records per local calendar month.

    Returns {"YYYY-MM": {"kwh": float, "charges": int, "cards": {label: kwh}}}.
    Records are de-duplicated by id; records without a parsable start time
    are skipped (they can't be assigned to a month).

    With `solar_fn` (record -> estimated solar kWh or None) every month also
    gets a "solar_kwh" total.
    """
    by_id = {getattr(r, "id", index): r for index, r in enumerate(records)}
    months: dict[str, dict] = {}
    for record in by_id.values():
        parsed = dt_util.parse_datetime(getattr(record, "start_date_time", "") or "")
        if parsed is None:
            continue
        local = dt_util.as_local(parsed)
        key = f"{local.year:04d}-{local.month:02d}"
        kwh = (getattr(record, "total_energy_wh", 0) or 0) / 1000
        entry = months.setdefault(key, {"kwh": 0.0, "charges": 0, "cards": {}})
        entry["kwh"] += kwh
        entry["charges"] += 1
        label = card_label(record)
        entry["cards"][label] = entry["cards"].get(label, 0.0) + kwh
        if solar_fn is not None:
            solar = solar_fn(record)
            if solar is not None:
                entry["solar_kwh"] = entry.get("solar_kwh", 0.0) + solar
    return months


def build_history(months: dict[str, dict]) -> list[dict]:
    """Turn {"YYYY-MM": agg} into a newest-first list of years with 12 months each."""
    years: dict[int, dict] = {}
    for key, agg in months.items():
        year, month = int(key[:4]), int(key[5:7])
        ydata = years.setdefault(
            year,
            {
                "year": year,
                "kwh": 0.0,
                "charges": 0,
                "cards": {},
                "months": [
                    {"month": m, "kwh": 0.0, "charges": 0, "cards": {}}
                    for m in range(1, 13)
                ],
            },
        )
        mdata = ydata["months"][month - 1]
        mdata["kwh"] += agg["kwh"]
        mdata["charges"] += agg["charges"]
        ydata["kwh"] += agg["kwh"]
        ydata["charges"] += agg["charges"]
        if "solar_kwh" in agg:
            mdata["solar_kwh"] = mdata.get("solar_kwh", 0.0) + agg["solar_kwh"]
            ydata["solar_kwh"] = ydata.get("solar_kwh", 0.0) + agg["solar_kwh"]
        for label, kwh in agg["cards"].items():
            mdata["cards"][label] = mdata["cards"].get(label, 0.0) + kwh
            ydata["cards"][label] = ydata["cards"].get(label, 0.0) + kwh

    def _round_cards(cards: dict) -> dict:
        return {k: round(v, 2) for k, v in sorted(cards.items(), key=lambda i: -i[1])}

    result = []
    for year in sorted(years, reverse=True):
        ydata = years[year]
        ydata["kwh"] = round(ydata["kwh"], 2)
        ydata["cards"] = _round_cards(ydata["cards"])
        if "solar_kwh" in ydata:
            ydata["solar_kwh"] = round(ydata["solar_kwh"], 2)
        for mdata in ydata["months"]:
            mdata["kwh"] = round(mdata["kwh"], 2)
            mdata["cards"] = _round_cards(mdata["cards"])
            if "solar_kwh" in mdata:
                mdata["solar_kwh"] = round(mdata["solar_kwh"], 2)
        result.append(ydata)
    return result


def most_recent_records(records, limit: int) -> list:
    """De-duplicate (by id) and return the `limit` most recent records.

    Sorted newest-first by `start_date_time`; records with a missing/
    unparsable timestamp sort last rather than raising.
    """
    by_id = {getattr(r, "id", index): r for index, r in enumerate(records)}

    _EPOCH = datetime(1970, 1, 1, tzinfo=dt_util.UTC)

    def _sort_key(record):
        parsed = dt_util.parse_datetime(getattr(record, "start_date_time", "") or "")
        return parsed or _EPOCH

    ordered = sorted(by_id.values(), key=_sort_key, reverse=True)
    return ordered[:limit]


# --------------------------------------------------------------------------
# Solar share (v1.6.0)
# --------------------------------------------------------------------------
HOUR = 3600


def record_window(record) -> tuple[float, float] | None:
    """Return (start, stop) of a charge as UTC epoch seconds, or None."""
    start = dt_util.parse_datetime(getattr(record, "start_date_time", "") or "")
    stop = dt_util.parse_datetime(getattr(record, "stop_date_time", "") or "")
    if start is None or stop is None:
        return None
    start_ts, stop_ts = start.timestamp(), stop.timestamp()
    if stop_ts <= start_ts:
        return None
    return start_ts, stop_ts


def estimate_solar_kwh(record, hourly_pv: dict[int, float]) -> float | None:
    """Estimate how much of one charge could have been covered by solar.

    `hourly_pv` maps the UTC epoch second of each hour's start to the PV
    energy produced in that hour (kWh, from Home Assistant's long-term
    statistics). The charge's energy is spread evenly over its duration and,
    hour by hour, capped by the PV energy produced during the overlapping
    part of that hour.

    This is an UPPER BOUND: it assumes all PV energy during the charge went
    into the car (the house's own consumption isn't known here). Returns
    None if the record has no usable start/stop time, or if there are no PV
    statistics at all for the charge's time span (e.g. before the PV system
    or its sensor existed) - "unknown" rather than a misleading 0 %.
    """
    window = record_window(record)
    if window is None:
        return None
    start_ts, stop_ts = window
    total_kwh = (getattr(record, "total_energy_wh", 0) or 0) / 1000
    if total_kwh <= 0:
        return 0.0
    duration = stop_ts - start_ts
    solar = 0.0
    covered = False
    hour = int(start_ts // HOUR) * HOUR
    while hour < stop_ts:
        overlap = min(stop_ts, hour + HOUR) - max(start_ts, hour)
        if overlap > 0 and hour in hourly_pv:
            covered = True
            charge_part = total_kwh * overlap / duration
            pv_part = max(hourly_pv[hour], 0.0) * overlap / HOUR
            solar += min(charge_part, pv_part)
        hour += HOUR
    if not covered:
        return None
    return min(solar, total_kwh)


def hourly_from_statistics(rows) -> dict[int, float]:
    """Turn recorder statistic rows (hourly, with "change") into {hour_ts: kWh}."""
    hourly: dict[int, float] = {}
    for row in rows or []:
        start = row.get("start")
        change = row.get("change")
        if start is None or change is None:
            continue
        if isinstance(start, datetime):
            start = start.timestamp()
        hourly[int(start)] = float(change)
    return hourly


def recent_charge_summary(record, solar_kwh: float | None = None) -> dict:
    """Compact, JSON-friendly summary of one charge for the dashboard card."""
    kwh = round((getattr(record, "total_energy_wh", 0) or 0) / 1000, 2)
    summary: dict = {
        "start": getattr(record, "start_date_time", None),
        "end": getattr(record, "stop_date_time", None),
        "kwh": kwh,
        "card": card_label(record),
    }
    window = record_window(record)
    if window is not None:
        minutes = (window[1] - window[0]) / 60
        summary["minutes"] = round(minutes)
        if minutes > 0:
            summary["avg_kw"] = round(kwh / (minutes / 60), 2)
    if solar_kwh is not None:
        summary["solar_kwh"] = round(solar_kwh, 2)
    return summary
