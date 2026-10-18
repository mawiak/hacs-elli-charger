"""Support for Elli Charger sensors."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .billing import async_get_payments
from .const import DATA_SESSIONS, DATA_STATIONS, DOMAIN
from .energy_periods import card_label

RECENT_CHARGES_COUNT = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Elli Charger sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    if stations := coordinator.data.get(DATA_STATIONS):
        for station in stations:
            station_id = station.id
            entities.append(ElliWallboxSensor(coordinator, station_id))
            entities.append(ElliLastSessionSensor(coordinator, station_id))
            entities.append(ElliSessionEnergySensor(coordinator, station_id))
            # Improvement #1: the README has always documented a "current
            # session power" sensor, but the integration never actually
            # created one - add the missing entity.
            entities.append(ElliSessionPowerSensor(coordinator, station_id))

            # Bonus feature: at-a-glance month/year totals, built from
            # Elli's historical records (same data the monthly PDF report
            # uses) rather than the live session endpoint.
            entities.append(ElliCurrentMonthEnergySensor(coordinator, station_id))
            entities.append(ElliLastMonthEnergySensor(coordinator, station_id))
            entities.append(ElliYearEnergySensor(coordinator, station_id))

            # Year -> month history for the custom:elli-history-card.
            entities.append(ElliEnergyHistorySensor(coordinator, station_id))

            # Bonus feature: the last 10 charges, like the list in the Elli
            # app - one sensor per rank (1 = most recent) since a Home
            # Assistant sensor only has a single state; date/time is the
            # state itself, everything else is an attribute.
            for rank in range(1, RECENT_CHARGES_COUNT + 1):
                entities.append(ElliRecentChargeSensor(coordinator, station_id, rank))

    async_add_entities(entities)


def session_status(session) -> str:
    """idle / connected / charging for the latest session of a station."""
    if not session:
        return "idle"
    if session.charging_state and "charging" in session.charging_state.lower():
        return "charging"
    if session.momentary_charging_speed_watts and session.momentary_charging_speed_watts > 0:
        return "charging"
    if session.lifecycle_state == "active":
        return "connected"
    return "idle"


class ElliStationEntity(CoordinatorEntity):
    """Common lookups + device grouping shared by all Elli sensors.

    Improvement #2: every sensor for a given wallbox is now attached to a
    single Home Assistant *device* (grouped by station), instead of showing
    up as a handful of unrelated, ungrouped entities.
    """

    # Combine with the device name so entities read as e.g. "Elli Wallbox
    # Garage Status" instead of a bare, out-of-context "Status".
    _attr_has_entity_name = True

    def __init__(self, coordinator, station_id: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._station_id = station_id

    def _get_station(self):
        """Get the station object."""
        stations = self.coordinator.data.get(DATA_STATIONS, [])
        for station in stations:
            if station.id == self._station_id:
                return station
        return None

    def _get_latest_session(self):
        """Get the latest session for this station."""
        sessions = self.coordinator.data.get(DATA_SESSIONS, [])
        for session in sessions:
            if session.station_id == self._station_id:
                return session
        return None

    @property
    def device_info(self) -> DeviceInfo:
        """Group this entity's sensors under one device per wallbox."""
        station = self._get_station()
        return DeviceInfo(
            identifiers={(DOMAIN, self._station_id)},
            name=station.name if station else f"Elli Wallbox {self._station_id}",
            manufacturer="Elli (Volkswagen Group Charging GmbH)",
            model=station.model if station else None,
            sw_version=station.firmware_version if station else None,
        )


class ElliWallboxSensor(ElliStationEntity, SensorEntity):
    """Representation of an Elli Wallbox sensor."""

    # Improvement #8: use a translation_key + enum device class instead of
    # hardcoded English literals, so the state is actually localized (the
    # integration already ships a de.json, it just never covered this).
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "wallbox_status"
    _attr_options = ["idle", "connected", "charging"]

    def __init__(self, coordinator, station_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, station_id)
        self._attr_icon = "mdi:ev-station"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self.coordinator.config_entry.entry_id}_wallbox_{self._station_id}"

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Status"

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        if self._is_charging():
            return "charging"
        if self._has_active_session():
            return "connected"
        return "idle"

    def _has_active_session(self) -> bool:
        """Check if station has an active session."""
        session = self._get_latest_session()
        return bool(session and session.lifecycle_state == "active")

    def _is_charging(self) -> bool:
        """Check if station is currently charging."""
        session = self._get_latest_session()
        if not session:
            return False
        if session.charging_state and "charging" in session.charging_state.lower():
            return True
        if session.momentary_charging_speed_watts and session.momentary_charging_speed_watts > 0:
            return True
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        station = self._get_station()
        if not station:
            return {}

        attrs = {
            "station_id": station.id,
            "name": station.name,
        }
        if station.serial_number:
            attrs["serial_number"] = station.serial_number
        if station.model:
            attrs["model"] = station.model
        if station.firmware_version:
            attrs["firmware_version"] = station.firmware_version

        return attrs


class ElliLastSessionSensor(ElliStationEntity, SensorEntity):
    """Representation of an Elli last/current session sensor."""

    def __init__(self, coordinator, station_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, station_id)
        self._attr_icon = "mdi:ev-plug-type2"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self.coordinator.config_entry.entry_id}_wallbox_{self._station_id}_last_session"

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Last Session"

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor (charging state)."""
        session = self._get_latest_session()
        if not session:
            return "idle"
        if session.charging_state:
            return session.charging_state
        if session.lifecycle_state:
            return session.lifecycle_state
        return "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        session = self._get_latest_session()
        if not session:
            return {}

        attrs = {
            "session_id": session.id,
            "station_id": session.station_id,
        }

        if session.energy_consumption_wh is not None:
            attrs["session_energy"] = round(session.energy_consumption_wh / 1000, 2)
        if session.momentary_charging_speed_watts is not None:
            attrs["session_power"] = session.momentary_charging_speed_watts

        if session.lifecycle_state:
            attrs["lifecycle_state"] = session.lifecycle_state
        if session.charging_state:
            attrs["charging_state"] = session.charging_state

        if session.authentication_method:
            attrs["authentication_method"] = session.authentication_method
        if session.rfid_card_serial_number:
            attrs["rfid_card"] = session.rfid_card_serial_number
        if session.connector_id:
            attrs["connector_id"] = session.connector_id

        if session.start_date_time:
            attrs["start_time"] = session.start_date_time
        if session.end_date_time:
            attrs["end_time"] = session.end_date_time
        if session.last_updated:
            attrs["last_updated"] = session.last_updated

        return attrs


class ElliSessionEnergySensor(ElliStationEntity, RestoreEntity, SensorEntity):
    """Representation of an Elli session energy sensor for the Energy Dashboard."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, station_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, station_id)
        self._attr_icon = "mdi:battery-charging"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self.coordinator.config_entry.entry_id}_wallbox_{self._station_id}_session_energy"

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Session Energy"

    async def async_added_to_hass(self) -> None:
        """Restore the last known value after a Home Assistant restart.

        Improvement #3: this sensor feeds the Energy Dashboard, which is
        sensitive to gaps. Previously the value dropped to `unknown`
        whenever no session happened to be active (or right after a
        restart), which the long-term statistics engine reads as a real
        gap. We now both (a) restore the last persisted value on startup
        and (b) fall back to the coordinator's cached last-known reading
        between sessions instead of returning None.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            None,
            "unknown",
            "unavailable",
        ):
            try:
                restored_wh = int(float(last_state.state) * 1000)
            except (TypeError, ValueError):
                return
            existing = self.coordinator.last_known_energy_wh.get(self._station_id, 0)
            self.coordinator.last_known_energy_wh[self._station_id] = max(
                existing, restored_wh
            )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor (energy in kWh)."""
        session = self._get_latest_session()
        if session and session.energy_consumption_wh is not None:
            return round(session.energy_consumption_wh / 1000, 2)

        last_known = self.coordinator.last_known_energy_wh.get(self._station_id)
        if last_known is not None:
            return round(last_known / 1000, 2)

        return None


class ElliSessionPowerSensor(ElliStationEntity, SensorEntity):
    """Representation of the current charging power (Improvement #1).

    The README has documented a `sensor.elli_current_session_power` entity
    since the very first release, but `sensor.py` never actually created
    one - the momentary charging speed was only ever exposed as an
    attribute on the last-session sensor.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, station_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, station_id)
        self._attr_icon = "mdi:flash"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self.coordinator.config_entry.entry_id}_wallbox_{self._station_id}_session_power"

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Session Power"

    @property
    def native_value(self) -> int:
        """Return the current momentary charging power in Watts."""
        session = self._get_latest_session()
        if session and session.momentary_charging_speed_watts is not None:
            return session.momentary_charging_speed_watts
        # No active session -> 0 W is more correct/continuous for a
        # MEASUREMENT power sensor than leaving it `unknown`.
        return 0


class ElliPeriodEnergySensor(ElliStationEntity, SensorEntity):
    """Base for the current-month / last-month / year-to-date sensors.

    Unlike `ElliSessionEnergySensor` (which only ever reflects the current
    or most recently finished session), these read the aggregates the
    coordinator periodically computes from Elli's historical *records*
    endpoint - the same data source the monthly PDF report uses (see
    `_async_refresh_energy_stats` in __init__.py and `energy_periods.py`).

    `TOTAL` (rather than `TOTAL_INCREASING`) is used deliberately: these
    values are recomputed from scratch on every stats refresh rather than
    accumulated locally, so an occasional dip (e.g. a slow API response
    that momentarily returns fewer records) should not be interpreted by
    Home Assistant's statistics engine as a meter reset.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    _stat_key: str = ""
    _sensor_name: str = ""
    _sensor_icon: str = "mdi:battery-charging"
    _id_suffix: str = ""

    def __init__(self, coordinator, station_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, station_id)
        self._attr_icon = self._sensor_icon

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return (
            f"{self.coordinator.config_entry.entry_id}_wallbox_"
            f"{self._station_id}_{self._id_suffix}"
        )

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._sensor_name

    @property
    def native_value(self) -> float | None:
        """Return the aggregated kWh value for this period."""
        stats = self.coordinator.energy_stats.get(self._station_id)
        if not stats:
            return None
        return stats.get(self._stat_key)


class ElliCurrentMonthEnergySensor(ElliPeriodEnergySensor):
    """Energy charged so far in the current calendar month."""

    _stat_key = "current_month_kwh"
    _sensor_name = "Energy This Month"
    _sensor_icon = "mdi:calendar-month"
    _id_suffix = "energy_this_month"


class ElliLastMonthEnergySensor(ElliPeriodEnergySensor):
    """Energy charged in the previous (now complete) calendar month."""

    _stat_key = "last_month_kwh"
    _sensor_name = "Energy Last Month"
    _sensor_icon = "mdi:calendar-month-outline"
    _id_suffix = "energy_last_month"


class ElliYearEnergySensor(ElliPeriodEnergySensor):
    """Energy charged so far in the current calendar year."""

    _stat_key = "year_kwh"
    _sensor_name = "Energy This Year"
    _sensor_icon = "mdi:calendar-star"
    _id_suffix = "energy_this_year"


class ElliEnergyHistorySensor(ElliStationEntity, SensorEntity):
    """All-time charged energy, with a year -> month breakdown as attributes.

    State: total kWh over all loaded years. The `years` attribute holds a
    newest-first list of years, each with totals, per-card totals and all
    12 months - this is what the bundled `custom:elli-history-card` (and
    e.g. apexcharts-card) renders. The attribute is excluded from the
    recorder so it doesn't bloat the database.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _unrecorded_attributes = frozenset(
        {"years", "recent_charges", "live", "loading", "next_refresh", "payments"}
    )

    def __init__(self, coordinator, station_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, station_id)
        self._attr_icon = "mdi:chart-bar"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return (
            f"{self.coordinator.config_entry.entry_id}_wallbox_"
            f"{self._station_id}_energy_history"
        )

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Energy History"

    def _history(self) -> list[dict] | None:
        return self.coordinator.energy_history.get(self._station_id)

    @property
    def native_value(self) -> float | None:
        """Return the all-time total in kWh."""
        history = self._history()
        if history is None:
            return None
        return round(sum(year["kwh"] for year in history), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the year/month breakdown."""
        history = self._history()
        if history is None:
            # v1.6.2: still loading - tell the dashboard card when the next
            # attempt is due so it can show a countdown instead of an error.
            nxt = self.coordinator.next_stats_attempt
            return {
                "loading": True,
                "next_refresh": nxt.isoformat() if nxt else None,
            }
        attrs: dict[str, Any] = {
            "years": history,
            "charges": sum(year["charges"] for year in history),
            "first_year": history[-1]["year"] if history else None,
            # v1.6.0 - consumed by custom:elli-history-card
            "live": self._live(),
            "recent_charges": self.coordinator.recent_charges.get(self._station_id, []),
        }
        if self.coordinator.solar_entity:
            attrs["solar_entity"] = self.coordinator.solar_entity
        # v1.6.4 - payment dates per month and card, for tenant billing.
        attrs["payments"] = {
            month: dict(cards) for month, cards in async_get_payments(self.hass).items()
        }
        return attrs

    def _live(self) -> dict[str, Any]:
        """Current session snapshot for the card's live header."""
        session = self._get_latest_session()
        status = session_status(session)
        live: dict[str, Any] = {"status": status}
        if session is None or status == "idle":
            return live
        live["power_w"] = session.momentary_charging_speed_watts or 0
        if session.energy_consumption_wh is not None:
            live["energy_kwh"] = round(session.energy_consumption_wh / 1000, 2)
        if session.start_date_time:
            live["start"] = session.start_date_time
        if session.rfid_card_serial_number:
            live["card"] = f"RFID {session.rfid_card_serial_number}"
        elif session.authentication_method:
            live["card"] = session.authentication_method
        return live


class ElliRecentChargeSensor(ElliStationEntity, SensorEntity):
    """One of the last 10 charges for this station, like the Elli app's history.

    Home Assistant sensors only ever have a single state, so a "list of the
    last 10 charges" is modeled as ten sensors (rank 1 = most recent) rather
    than one sensor with a list buried in its attributes - that way each
    charge's date/time is directly visible (and usable in the history graph
    / automations) instead of hidden inside an attribute.
    """

    # Compact layout (v1.4.0): the *name* carries the date/time
    # ("01.10.26 18:57"), the *state* is the energy as a real kWh number -
    # so the entity reads e.g. "01.10.26 18:57   57,4 kWh" instead of
    # repeating the whole "Charge 01.10.2026 18:57 - 57.36 kWh" string in
    # both name and state. No state_class: these are single charges, not a
    # meter, so they must not end up in long-term energy statistics.
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, station_id: str, rank: int) -> None:
        """Initialize the sensor. `rank` 1 is the most recent charge."""
        super().__init__(coordinator, station_id)
        self._rank = rank
        self._attr_icon = "mdi:history"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return (
            f"{self.coordinator.config_entry.entry_id}_wallbox_"
            f"{self._station_id}_recent_charge_{self._rank}"
        )

    @property
    def name(self) -> str:
        """Return the short start date/time, e.g. "01.10.26 18:57".

        Dynamic so each of the ten entities is identifiable at a glance;
        unique_id/entity_id stay fixed on `_rank`.
        """
        local_start = self._local_start()
        if local_start is None:
            return f"Ladung {self._rank}"
        return local_start.strftime("%d.%m.%y %H:%M")

    def _get_record(self):
        """Return this rank's ChargingRecord, or None if not (yet) available."""
        records = self.coordinator.recent_records.get(self._station_id, [])
        index = self._rank - 1
        if index < len(records):
            return records[index]
        return None

    def _local_start(self):
        """Return this charge's start as local datetime, or None."""
        record = self._get_record()
        if record is None or not record.start_date_time:
            return None
        parsed = dt_util.parse_datetime(record.start_date_time)
        return dt_util.as_local(parsed) if parsed else None

    def _format_label(self) -> str | None:
        """Return "DD.MM.YYYY HH:MM - X.XX kWh" (kept as attribute)."""
        record = self._get_record()
        local_start = self._local_start()
        if record is None or local_start is None:
            return None
        energy_kwh = round((record.total_energy_wh or 0) / 1000, 2)
        return f"{local_start.strftime('%d.%m.%Y %H:%M')} - {energy_kwh:.2f} kWh"

    @property
    def native_value(self) -> float | None:
        """Return the energy of this charge in kWh."""
        record = self._get_record()
        if record is None:
            return None
        return round((record.total_energy_wh or 0) / 1000, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        record = self._get_record()
        if record is None:
            return {}

        attrs: dict[str, Any] = {
            "energy_kwh": round((record.total_energy_wh or 0) / 1000, 2),
            "rank": self._rank,
        }
        if label := self._format_label():
            attrs["label"] = label
        if record.start_date_time:
            attrs["start_time"] = record.start_date_time
        if record.stop_date_time:
            attrs["end_time"] = record.stop_date_time
        attrs["card"] = card_label(record)
        if getattr(record, "station_name", None):
            attrs["station_name"] = record.station_name
        if getattr(record, "session_faulted", False):
            attrs["session_faulted"] = True

        return attrs
