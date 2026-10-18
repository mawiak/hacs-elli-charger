"""Monthly kWh-per-RFID-card report for the Elli Charger integration.

Bonus feature (requested on top of the 10 code-quality/robustness
improvements): a Home Assistant *service* that builds a one-page PDF
showing, for a given month, how many kWh were charged on each RFID
card/authentication method, and optionally emails it out via any already
configured `notify` service (e.g. the built-in SMTP notify platform).

Data source note: `get_charging_sessions()` (used by the sensors) only ever
returns the *active/ongoing* session per station, not history - it cannot
answer "how much did card X charge last month". This report instead calls
`get_charging_records()`, Elli's actual historical-records endpoint, once
per station for the requested month (with pagination).

Usage from Developer Tools -> Actions, a script, or an automation
(e.g. triggered on the 1st of every month):

    action: elli_charger.send_monthly_report
    data:
      month: "2026-08"                     # optional, defaults to last month
      notify_service: notify.email_family  # optional, defaults to notify.notify
      target: "you@example.com"            # optional, forwarded to the notify service

The service always returns the path of the generated PDF (also announced
via a persistent notification), so it's useful even without email set up.
"""
from __future__ import annotations

import calendar
import logging
import os
from collections import defaultdict
from datetime import timedelta

import voluptuous as vol

from elli_client import ReauthenticationRequired
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DATA_STATIONS, DEFAULT_REPORT_NOTIFY_SERVICE, DOMAIN
from .energy_periods import card_label, fetch_station_records, month_bounds_utc

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_MONTHLY_REPORT = "send_monthly_report"

ATTR_MONTH = "month"
ATTR_NOTIFY_SERVICE = "notify_service"
ATTR_TARGET = "target"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MONTH): cv.matches_regex(r"^\d{4}-(0[1-9]|1[0-2])$"),
        vol.Optional(ATTR_NOTIFY_SERVICE, default=DEFAULT_REPORT_NOTIFY_SERVICE): cv.string,
        vol.Optional(ATTR_TARGET): cv.string,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def async_register_report_service(hass: HomeAssistant) -> None:
    """Register the send_monthly_report service, once."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_MONTHLY_REPORT):
        return

    async def _handle_send_monthly_report(call: ServiceCall) -> ServiceResponse:
        return await _async_send_monthly_report(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MONTHLY_REPORT,
        _handle_send_monthly_report,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _resolve_year_month(month: str | None) -> tuple[int, int]:
    """Return (year, month) for the requested/default report period."""
    if month:
        year_str, month_str = month.split("-")
        return int(year_str), int(month_str)

    now = dt_util.now()
    # Default to *last* month, since the current month is still incomplete.
    first_of_this_month = now.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    return last_month_end.year, last_month_end.month


async def _async_send_monthly_report(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Build the monthly per-card kWh PDF and optionally email it."""
    coordinators = hass.data.get(DOMAIN, {})
    if not coordinators:
        raise HomeAssistantError("No Elli Charger account is configured")

    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if entry_id:
        selected = {entry_id: coordinators[entry_id]} if entry_id in coordinators else {}
        if not selected:
            raise HomeAssistantError(f"Unknown config_entry_id: {entry_id}")
        coordinators = selected

    year, month = _resolve_year_month(call.data.get(ATTR_MONTH))
    created_after, created_before = month_bounds_utc(year, month)

    totals: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"energy_kwh": 0.0, "sessions": 0}
    )

    for coordinator in coordinators.values():
        # Make sure we have a live access token before the (potentially
        # multi-request, multi-station) historical fetch below.
        try:
            if coordinator._token_needs_refresh():  # noqa: SLF001 - same integration
                await coordinator.async_refresh_tokens()
        except ReauthenticationRequired as err:
            raise HomeAssistantError(
                "Elli Charger needs to be re-authenticated (see the "
                "Reauthenticate notification) before a report can be built."
            ) from err

        stations = coordinator.data.get(DATA_STATIONS, []) if coordinator.data else []
        for station in stations:
            try:
                records = await hass.async_add_executor_job(
                    fetch_station_records,
                    coordinator.client,
                    station.id,
                    created_after,
                    created_before,
                )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error(
                    "Could not fetch charging records for station %s: %s",
                    station.id,
                    err,
                )
                continue

            for record in records:
                label = card_label(record)
                totals[label]["energy_kwh"] += (record.total_energy_wh or 0) / 1000
                totals[label]["sessions"] += 1

    pdf_path = await hass.async_add_executor_job(
        _build_pdf, hass.config.path("www", "elli_charger"), year, month, totals
    )

    result: dict[str, str | float] = {
        "file_path": pdf_path,
        "month": f"{year:04d}-{month:02d}",
        "total_kwh": round(sum(v["energy_kwh"] for v in totals.values()), 2),
        "cards": len(totals),
    }

    notify_service = call.data.get(ATTR_NOTIFY_SERVICE, DEFAULT_REPORT_NOTIFY_SERVICE)
    if notify_service:
        await _async_send_via_notify(
            hass, notify_service, call.data.get(ATTR_TARGET), pdf_path, year, month, result
        )

    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "Elli Charger - Monatsbericht",
            "message": (
                f"Monatsbericht {year:04d}-{month:02d} erstellt: "
                f"{result['total_kwh']} kWh über {result['cards']} Karte(n).\n"
                f"Datei: {pdf_path}"
            ),
            "notification_id": "elli_charger_monthly_report",
        },
        blocking=False,
    )

    return result


def _build_pdf(
    target_dir: str,
    year: int,
    month: int,
    totals: dict[str, dict[str, float | int]],
) -> str:
    """Render the PDF report (runs in the executor - blocking I/O)."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as err:  # pragma: no cover - dependency documented in manifest.json
        raise HomeAssistantError(
            "The 'reportlab' package is required for PDF reports. "
            "Restart Home Assistant after updating the integration so its "
            "requirements can be installed."
        ) from err

    os.makedirs(target_dir, exist_ok=True)
    month_name = calendar.month_name[month]
    file_path = os.path.join(target_dir, f"monatsbericht_{year:04d}-{month:02d}.pdf")

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(file_path, pagesize=A4)
    story = [
        Paragraph(f"Elli Charger - Ladeübersicht {month_name} {year}", styles["Title"]),
        Spacer(1, 0.5 * cm),
    ]

    if not totals:
        story.append(Paragraph("Keine Ladevorgänge in diesem Zeitraum.", styles["Normal"]))
    else:
        data = [["Ladekarte / Methode", "Sitzungen", "Energie (kWh)"]]
        total_kwh = 0.0
        total_sessions = 0
        for label, values in sorted(totals.items(), key=lambda kv: -kv[1]["energy_kwh"]):
            data.append(
                [label, str(values["sessions"]), f"{values['energy_kwh']:.2f}"]
            )
            total_kwh += values["energy_kwh"]
            total_sessions += values["sessions"]
        data.append(["Gesamt", str(total_sessions), f"{total_kwh:.2f}"])

        table = Table(data, colWidths=[8 * cm, 3 * cm, 4 * cm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E7D32")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
                    ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ]
            )
        )
        story.append(table)

    doc.build(story)
    return file_path


async def _async_send_via_notify(
    hass: HomeAssistant,
    notify_service: str,
    target: str | None,
    pdf_path: str,
    year: int,
    month: int,
    result: dict,
) -> None:
    """Send the PDF through an existing `notify.*` service, if available."""
    if "." not in notify_service:
        _LOGGER.warning(
            "notify_service %r is not in the form 'notify.<service>' - skipping email",
            notify_service,
        )
        return

    domain, service = notify_service.split(".", 1)
    if not hass.services.has_service(domain, service):
        _LOGGER.warning(
            "Notify service %s is not available - PDF was created at %s but not sent. "
            "Set up an email/notify integration (e.g. the SMTP notify platform) first.",
            notify_service,
            pdf_path,
        )
        return

    data = {
        "title": f"Elli Charger Monatsbericht {year:04d}-{month:02d}",
        "message": (
            f"{result['total_kwh']} kWh geladen über {result['cards']} Karte(n) "
            f"im {month:02d}/{year}."
        ),
        # The built-in SMTP notify platform accepts arbitrary files (not
        # just images) under the "images" key and attaches them to the
        # email. Other notify platforms may ignore this key.
        "data": {"images": [pdf_path]},
    }
    if target:
        data["target"] = [target]

    await hass.services.async_call(domain, service, data, blocking=True)
