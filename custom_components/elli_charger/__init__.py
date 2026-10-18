"""The Elli Charger integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from elli_client import ElliAPIClient, ElliClientError, ReauthenticationRequired

from .const import (
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SOLAR_ENTITY,
    DATA_SESSIONS,
    DATA_STATIONS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    FIRMWARE_POLL_EVERY_N_CYCLES,
    HISTORY_CARD_URL,
    HISTORY_CARD_VERSION,
    HISTORY_MAX_QUICK_RETRIES,
    HISTORY_RETRY_SECONDS,
    HISTORY_MAX_YEARS_BACK,
    STATS_POLL_EVERY_N_CYCLES,
    TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS,
)
from .energy_periods import (
    build_history,
    estimate_solar_kwh,
    hourly_from_statistics,
    recent_charge_summary,
    fetch_station_records,
    group_records_by_month,
    local_bounds_to_utc,
    month_bounds_utc,
    most_recent_records,
    sum_energy_kwh,
    year_bounds_utc,
)
from .billing import async_setup_billing
from .report import SERVICE_SEND_MONTHLY_REPORT, async_register_report_service

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


def _looks_like_auth_error(err: Exception) -> bool:
    """Best-effort check whether a *data* API error is an auth failure.

    `get_stations()` / `get_charging_sessions()` / `get_firmware_info()`
    still raise a bare ValueError on any non-200 response (unlike the OAuth
    methods, which got proper exception types in elli-client 1.5.0), so we
    have to sniff the message for a 401 hint.
    """
    message = str(err).lower()
    return "401" in message or "unauthorized" in message


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the dashboard card as early as possible.

    v1.6.2: the card used to be registered at the very end of the entry
    setup - i.e. only after the (slow) first download of the charging
    history. Dashboards opened during that time showed Home Assistant's
    "configuration error" for an unknown custom card. Registering it here,
    before any network traffic, makes the card available right away; it
    then shows a friendly "loading" message until the data has arrived.
    """
    await _async_register_history_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Elli Charger from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    # Idempotent - normally already done in async_setup().
    await _async_register_history_card(hass)

    if CONF_REFRESH_TOKEN not in entry.data:
        # Pre-1.2.0 entries stored an email/password, which the new
        # browser-based login flow (required since Elli added a Cloudflare
        # Turnstile challenge) cannot use. Fail clearly instead of a raw
        # KeyError, and point at the fix.
        _LOGGER.error(
            "This Elli Charger entry still uses the old email/password setup, "
            "which no longer works. Remove and re-add the integration to "
            "complete the new browser-based login."
        )
        raise ConfigEntryAuthFailed("Legacy config entry - reauthentication required")

    # Confirmed via a real "blocking call" warning from Home Assistant's
    # event-loop guard: ElliAPIClient() builds an httpx.Client, which loads
    # the system TLS certificate store synchronously. Build it in the
    # executor rather than directly on the event loop.
    client = await hass.async_add_executor_job(ElliAPIClient)

    coordinator = ElliDataUpdateCoordinator(hass, client, entry)

    # Perform the very first token refresh outside of the coordinator's own
    # retry handling so a revoked/expired refresh token surfaces immediately
    # as a setup failure (-> reauth) instead of a silently "unavailable"
    # integration.
    try:
        await coordinator.async_refresh_tokens()
    except ReauthenticationRequired as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error("Failed to refresh Elli tokens: %s", err)
        return False

    # v1.6.2: the first refresh only reads the cheap live endpoints so the
    # integration is up within seconds; the charging history (one request
    # per year, can take a while) is loaded right afterwards in the
    # background and retried every few minutes until it is complete.
    coordinator.defer_stats = True
    await coordinator.async_config_entry_first_refresh()
    coordinator.defer_stats = False

    hass.data[DOMAIN][entry.entry_id] = coordinator
    entry.async_on_unload(coordinator.cancel_stats_retry)

    # Improvement #7: react to changes made through the Options Flow (e.g. a
    # new polling interval) without requiring a Home Assistant restart.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Forward the setup to the sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Bonus feature: register the monthly kWh-per-RFID-card report service
    # (idempotent - only actually registers on the first config entry).
    async_register_report_service(hass)

    # v1.6.4: tenant billing (payment dates + yearly PDF report).
    await async_setup_billing(hass)

    # Load the charging history now, without blocking Home Assistant's start.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} initial history load"
    )

    return True


async def _async_register_history_card(hass: HomeAssistant) -> None:
    """Serve the bundled Lovelace card and load it in every dashboard.

    Idempotent and best-effort: a problem here must never prevent the
    integration (and its sensors) from loading.
    """
    flag = f"{DOMAIN}_history_card_registered"
    if hass.data.get(flag):
        return
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        card_path = Path(__file__).parent / "www" / "elli-history-card.js"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(HISTORY_CARD_URL, str(card_path), False)]
        )
        add_extra_js_url(hass, f"{HISTORY_CARD_URL}?v={HISTORY_CARD_VERSION}")
        hass.data[flag] = True
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.warning("Could not register the Elli history card: %s", err)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update (e.g. scan interval changed in the UI)."""
    coordinator: ElliDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if (entry.options.get(CONF_SOLAR_ENTITY) or None) != coordinator.solar_entity:
        # A different PV sensor changes all (cached) history figures - the
        # simplest correct thing is a full reload of the entry.
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    coordinator.update_interval = timedelta(
        minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: ElliDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(coordinator.client.close)

    # If this was the last Elli Charger entry, remove the (global) service.
    if not hass.data.get(DOMAIN) and hass.services.has_service(
        DOMAIN, SERVICE_SEND_MONTHLY_REPORT
    ):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_MONTHLY_REPORT)

    return unload_ok


class ElliDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Elli data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ElliAPIClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_interval),
            config_entry=entry,
        )
        self.client = client
        self._refresh_token = entry.data[CONF_REFRESH_TOKEN]

        # Improvement #4: track when the current access token actually
        # expires (elli-client now computes this for us) so we can refresh
        # proactively instead of only reacting to a 401.
        self._token_expires_at = None

        # Improvement #6: only poll the (rarely changing) firmware endpoint
        # every Nth cycle; cache the result in between.
        self._cycle_count: int = 0
        self._firmware_map: dict[str, object] = {}

        # Improvement #3: remember the last known energy reading per station
        # so the session-energy sensor never has to report "unknown" just
        # because no session happens to be active right now.
        self.last_known_energy_wh: dict[str, int] = {}

        # Bonus feature: "energy this month / last month / this year" and
        # "last 10 charges" sensors. These read Elli's historical *records*
        # endpoint (the same one the monthly PDF report uses) rather than
        # the cheap "current session" endpoint, so they're refreshed on a
        # much slower cadence - see STATS_POLL_EVERY_N_CYCLES.
        self.energy_stats: dict[str, dict[str, float]] = {}
        self.recent_records: dict[str, list] = {}
        self._cached_last_month_key: tuple[int, int] | None = None
        self._cached_last_month_kwh: dict[str, float] = {}
        self._cached_last_month_records: dict[str, list] = {}

        # Year/month history ("Energy History" sensor + dashboard card).
        # Completed years never change, so they are fetched once and kept
        # as compact per-month aggregates; only the current year is
        # re-read on every stats cycle (it already is, for year_kwh).
        self.energy_history: dict[str, list[dict]] = {}
        # v1.6.0: last charges as compact dicts (for the dashboard card) and
        # the optional PV sensor for the solar-share estimate.
        self.recent_charges: dict[str, list[dict]] = {}
        self.solar_entity: str | None = entry.options.get(CONF_SOLAR_ENTITY) or None
        self._past_months: dict[str, dict[str, dict]] = {}
        self._history_year: int | None = None

        # v1.6.2: history is loaded after setup (defer_stats) and, until it
        # is complete, retried every HISTORY_RETRY_SECONDS instead of only
        # on the hourly stats cycle. next_stats_attempt is shown by the card.
        self.defer_stats = False
        self._force_stats = True
        self.next_stats_attempt: datetime | None = None
        self._stats_retry_unsub: CALLBACK_TYPE | None = None
        self._stats_failures = 0

    @callback
    def cancel_stats_retry(self) -> None:
        """Cancel a pending history retry (entry unload)."""
        if self._stats_retry_unsub is not None:
            self._stats_retry_unsub()
            self._stats_retry_unsub = None

    @callback
    def _schedule_stats_retry(self) -> None:
        """Try loading the history again in a few minutes.

        After HISTORY_MAX_QUICK_RETRIES failed attempts in a row, fall back
        to the normal hourly statistics cycle so a persistent API problem
        doesn't hit Elli every few minutes all day.
        """
        self.cancel_stats_retry()
        self._stats_failures += 1
        if self._stats_failures > HISTORY_MAX_QUICK_RETRIES:
            if self._stats_failures == HISTORY_MAX_QUICK_RETRIES + 1:
                _LOGGER.warning(
                    "Elli charging history still incomplete after %s attempts - "
                    "retrying with the hourly statistics update from now on",
                    HISTORY_MAX_QUICK_RETRIES,
                )
            self._force_stats = False
            self.next_stats_attempt = None
            return
        self._force_stats = True
        self.next_stats_attempt = dt_util.utcnow() + timedelta(
            seconds=HISTORY_RETRY_SECONDS
        )

        @callback
        def _retry(_now) -> None:
            self._stats_retry_unsub = None
            self.hass.async_create_task(self.async_request_refresh())

        self._stats_retry_unsub = async_call_later(
            self.hass, HISTORY_RETRY_SECONDS, _retry
        )

    async def async_refresh_tokens(self) -> None:
        """Refresh the access token using the stored refresh token.

        Raises `ReauthenticationRequired` (from elli_client) if the refresh
        token itself is no longer valid - the caller is expected to turn
        that into `ConfigEntryAuthFailed`.
        """
        tokens = await self.hass.async_add_executor_job(
            self.client.refresh, self._refresh_token
        )
        self.client.set_tokens(tokens)
        self._token_expires_at = tokens.expires_at

        # Auth0 may rotate refresh tokens on use - persist the new one so we
        # don't get stuck with a stale/invalidated one on the next restart.
        if tokens.refresh_token and tokens.refresh_token != self._refresh_token:
            self._refresh_token = tokens.refresh_token
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                },
            )

    def _token_needs_refresh(self) -> bool:
        if not self.client.access_token or self._token_expires_at is None:
            return True
        margin = timedelta(seconds=TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS)
        return dt_util.utcnow() >= (self._token_expires_at - margin)

    async def _async_solar_hourly(
        self, start: datetime, end: datetime
    ) -> dict[int, float] | None:
        """Hourly PV production (kWh) from HA's long-term statistics, or None.

        None means "no PV sensor configured / statistics unavailable" - the
        solar share is then simply left out rather than shown as 0 %.
        """
        if not self.solar_entity:
            return None
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
            from homeassistant.const import UnitOfEnergy

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                dt_util.as_utc(start),
                dt_util.as_utc(end),
                {self.solar_entity},
                "hour",
                {"energy": UnitOfEnergy.KILO_WATT_HOUR},
                {"change"},
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Could not read PV statistics for %s: %s", self.solar_entity, err
            )
            return None
        return hourly_from_statistics(stats.get(self.solar_entity, []))

    async def _async_refresh_energy_stats(self, stations) -> None:
        """Recompute the month/last-month/year totals and last-10-charges list.

        Runs only every STATS_POLL_EVERY_N_CYCLES cycles, and even then does
        as few requests as it can: the (potentially large) previous-month
        fetch is only repeated once, right after the month actually rolls
        over - every other cycle reuses the cached figure/records.
        """
        now = dt_util.now()
        local_tz = dt_util.DEFAULT_TIME_ZONE
        year_after, year_before = local_bounds_to_utc(
            datetime(now.year, 1, 1, tzinfo=local_tz), now
        )
        month_after, month_before = local_bounds_to_utc(
            datetime(now.year, now.month, 1, tzinfo=local_tz), now
        )

        last_month_date = datetime(now.year, now.month, 1, tzinfo=local_tz) - timedelta(
            days=1
        )
        last_month_key = (last_month_date.year, last_month_date.month)
        need_last_month = last_month_key != self._cached_last_month_key
        if need_last_month:
            last_month_after, last_month_before = month_bounds_utc(*last_month_key)

        # PV production since Jan 1st (plus one month, so the "last 10
        # charges" in early January still get a solar estimate).
        hourly_pv = await self._async_solar_hourly(
            datetime(now.year, 1, 1, tzinfo=local_tz) - timedelta(days=31), now
        )

        def solar_fn(record):
            return estimate_solar_kwh(record, hourly_pv) if hourly_pv is not None else None

        if self._history_year != now.year:
            # New year (or first run): last year is complete now - reload.
            self._past_months = {}
            self._history_year = now.year

        for station in stations:
            try:
                year_records = await self.hass.async_add_executor_job(
                    fetch_station_records, self.client, station.id, year_after, year_before
                )
                month_records = await self.hass.async_add_executor_job(
                    fetch_station_records, self.client, station.id, month_after, month_before
                )
                if need_last_month:
                    self._cached_last_month_records[station.id] = (
                        await self.hass.async_add_executor_job(
                            fetch_station_records,
                            self.client,
                            station.id,
                            last_month_after,
                            last_month_before,
                        )
                    )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Could not update Elli energy statistics for station %s: %s",
                    station.id,
                    err,
                )
                continue

            self.energy_stats[station.id] = {
                "current_month_kwh": round(sum_energy_kwh(month_records), 2),
                "last_month_kwh": round(
                    sum_energy_kwh(self._cached_last_month_records.get(station.id, [])), 2
                ),
                "year_kwh": round(sum_energy_kwh(year_records), 2),
            }

            # Early in January "this year" alone may hold fewer than 10
            # charges - fold in the (already fetched/cached) last month too
            # so the "last 10 charges" sensors still fill up correctly.
            combined = list(year_records) + list(
                self._cached_last_month_records.get(station.id, [])
            )
            self.recent_records[station.id] = most_recent_records(combined, limit=10)
            self.recent_charges[station.id] = [
                recent_charge_summary(record, solar_fn(record))
                for record in self.recent_records[station.id]
            ]

            if station.id not in self._past_months:
                try:
                    self._past_months[station.id] = await self._async_load_past_years(
                        station.id, now.year
                    )
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.warning(
                        "Could not load Elli charging history for station %s "
                        "(will retry): %s",
                        station.id,
                        err,
                    )
            months = dict(self._past_months.get(station.id, {}))
            months.update(
                group_records_by_month(
                    year_records, solar_fn if hourly_pv is not None else None
                )
            )
            self.energy_history[station.id] = build_history(months)

        if need_last_month:
            self._cached_last_month_key = last_month_key

        complete = all(
            station.id in self.energy_history and station.id in self._past_months
            for station in stations
        )
        if complete:
            self.cancel_stats_retry()
            self._force_stats = False
            self.next_stats_attempt = None
            self._stats_failures = 0
        else:
            _LOGGER.info(
                "Elli charging history not complete yet, retrying in %s s",
                HISTORY_RETRY_SECONDS,
            )
            self._schedule_stats_retry()

    async def _async_load_past_years(
        self, station_id: str, current_year: int
    ) -> dict[str, dict]:
        """Fetch completed years (newest first) until the first empty year."""
        months: dict[str, dict] = {}
        for year in range(
            current_year - 1, current_year - 1 - HISTORY_MAX_YEARS_BACK, -1
        ):
            after, before = year_bounds_utc(year)
            records = await self.hass.async_add_executor_job(
                fetch_station_records, self.client, station_id, after, before
            )
            if not records:
                break
            local_tz = dt_util.DEFAULT_TIME_ZONE
            hourly_pv = await self._async_solar_hourly(
                datetime(year, 1, 1, tzinfo=local_tz),
                datetime(year + 1, 1, 1, tzinfo=local_tz),
            )
            solar_fn = (
                (lambda record, pv=hourly_pv: estimate_solar_kwh(record, pv))
                if hourly_pv is not None
                else None
            )
            months.update(group_records_by_month(records, solar_fn))
        return months

    async def _async_fetch_all(self, *, poll_firmware: bool, poll_stats: bool) -> dict:
        """Fetch sessions/stations (and optionally firmware/stats) in one place.

        Improvement #9: this used to be duplicated almost verbatim between
        the "happy path" and the "retry after re-auth" path.
        """
        sessions = await self.hass.async_add_executor_job(
            self.client.get_charging_sessions
        )
        stations = await self.hass.async_add_executor_job(self.client.get_stations)

        if poll_firmware:
            try:
                firmware_stations = await self.hass.async_add_executor_job(
                    self.client.get_firmware_info
                )
                self._firmware_map = {
                    s.id: s.installed_firmware
                    for s in firmware_stations
                    if s.installed_firmware
                }
            except Exception as fw_err:  # pylint: disable=broad-except
                _LOGGER.warning("Could not fetch firmware info: %s", fw_err)

        for station in stations:
            firmware = self._firmware_map.get(station.id)
            if firmware:
                station.installed_firmware = firmware
                station.firmware_version = firmware.version

        for session in sessions:
            if session.energy_consumption_wh is not None:
                self.last_known_energy_wh[session.station_id] = max(
                    self.last_known_energy_wh.get(session.station_id, 0),
                    session.energy_consumption_wh,
                )

        if poll_stats:
            await self._async_refresh_energy_stats(stations)

        return {DATA_SESSIONS: sessions, DATA_STATIONS: stations}

    async def _async_update_data(self) -> dict:
        """Fetch data from the API, refreshing the access token as needed."""
        self._cycle_count += 1
        poll_firmware = (self._cycle_count % FIRMWARE_POLL_EVERY_N_CYCLES) == 1
        poll_stats = (
            self._force_stats
            or (self._cycle_count % STATS_POLL_EVERY_N_CYCLES) == 1
        ) and not self.defer_stats
        if self.defer_stats:
            # Count the setup refresh as "cycle 0" so the hourly stats cycle
            # still starts with the background load right after setup.
            self._cycle_count -= 1
            poll_firmware = True

        if self._token_needs_refresh():
            try:
                await self.async_refresh_tokens()
            except ReauthenticationRequired as err:
                # Improvement #5: the refresh token itself is dead - asking
                # Home Assistant for a "Reauthenticate" prompt is the only
                # sane move; retrying on a timer would just fail forever.
                raise ConfigEntryAuthFailed(str(err)) from err
            except ElliClientError as err:
                # Anything else from the token endpoint (network hiccup,
                # unexpected response, ...) is transient - surface it as a
                # normal failed update, not an unhandled exception, and try
                # again next cycle with the still-valid-for-now token.
                raise UpdateFailed(f"Could not refresh Elli access token: {err}") from err

        try:
            return await self._async_fetch_all(
                poll_firmware=poll_firmware, poll_stats=poll_stats
            )
        except Exception as err:  # pylint: disable=broad-except
            if not _looks_like_auth_error(err):
                if poll_stats and self.next_stats_attempt is None:
                    self._schedule_stats_retry()
                raise UpdateFailed(f"Error communicating with API: {err}") from err

            _LOGGER.warning(
                "Elli API rejected our access token, refreshing once and retrying: %s",
                err,
            )
            try:
                await self.async_refresh_tokens()
                return await self._async_fetch_all(
                    poll_firmware=poll_firmware, poll_stats=poll_stats
                )
            except ReauthenticationRequired as retry_err:
                raise ConfigEntryAuthFailed(str(retry_err)) from retry_err
            except Exception as retry_err:  # pylint: disable=broad-except
                raise UpdateFailed(
                    f"Error communicating with API: {retry_err}"
                ) from retry_err
