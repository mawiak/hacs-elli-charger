"""Tenant billing for the Elli Charger integration (v1.6.4).

Two parts:

* **Payment dates** - for every month and RFID card the date on which the
  tenant paid the charging costs can be recorded. They are stored in
  Home Assistant's own storage (``.storage/elli_charger.payments``), exposed
  on the *Energy History* sensor (attribute ``payments``) and edited from
  the dashboard card through the service ``elli_charger.set_payment``.

* **Billing report** - ``elli_charger.create_billing_report`` renders a PDF
  for one year: monthly kWh, amount and payment date per card/tenant, with
  totals and the open amount. It is built from the already loaded charging
  history (no extra Elli API calls). The file is stored outside ``www`` and
  served only to logged-in users via ``/api/elli_charger/report/<file>``,
  because it contains tenant names and amounts.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date

import voluptuous as vol
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.http import KEY_HASS
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_PAYMENTS = f"{DOMAIN}_payments"
DATA_PAYMENTS_STORE = f"{DOMAIN}_payments_store"
DATA_BILLING_REGISTERED = f"{DOMAIN}_billing_registered"

STORAGE_KEY = f"{DOMAIN}.payments"
STORAGE_VERSION = 1

SERVICE_SET_PAYMENT = "set_payment"
SERVICE_CREATE_BILLING_REPORT = "create_billing_report"

REPORT_DIR = "elli_charger_reports"
REPORT_VIEW_URL = "/api/elli_charger/report/{filename}"
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+\.pdf$")

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

SET_PAYMENT_SCHEMA = vol.Schema(
    {
        vol.Required("month"): cv.matches_regex(r"^\d{4}-(0[1-9]|1[0-2])$"),
        vol.Required("card"): vol.All(cv.string, vol.Length(min=1, max=120)),
        # Empty / missing -> payment date is removed again.
        vol.Optional("paid_on"): vol.Any(None, "", cv.date),
    }
)

REPORT_SCHEMA = vol.Schema(
    {
        vol.Optional("year"): vol.All(vol.Coerce(int), vol.Range(min=2000, max=2100)),
        vol.Optional("price_per_kwh", default=0): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
        vol.Optional("currency", default="€"): vol.All(cv.string, vol.Length(max=5)),
        vol.Optional("card_names", default={}): {cv.string: cv.string},
        vol.Optional("title"): vol.All(cv.string, vol.Length(max=120)),
    }
)


# --------------------------------------------------------------------------- setup
async def async_setup_billing(hass: HomeAssistant) -> None:
    """Load stored payments and register services + report view (once)."""
    if hass.data.get(DATA_BILLING_REGISTERED):
        return
    hass.data[DATA_BILLING_REGISTERED] = True

    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    hass.data[DATA_PAYMENTS_STORE] = store
    hass.data[DATA_PAYMENTS] = (await store.async_load()) or {}

    async def _set_payment(call: ServiceCall) -> None:
        await async_set_payment(
            hass, call.data["month"], call.data["card"], call.data.get("paid_on")
        )

    async def _create_report(call: ServiceCall) -> ServiceResponse:
        return await async_create_billing_report(hass, dict(call.data))

    hass.services.async_register(
        DOMAIN, SERVICE_SET_PAYMENT, _set_payment, schema=SET_PAYMENT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_BILLING_REPORT,
        _create_report,
        schema=REPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.http.register_view(BillingReportView())


@callback
def async_get_payments(hass: HomeAssistant) -> dict[str, dict[str, str]]:
    """Return {"YYYY-MM": {card_label: "YYYY-MM-DD"}}."""
    return hass.data.get(DATA_PAYMENTS, {})


async def async_set_payment(
    hass: HomeAssistant, month: str, card: str, paid_on: date | str | None
) -> None:
    """Store (or clear) the payment date of one month/card and refresh sensors."""
    payments = hass.data.setdefault(DATA_PAYMENTS, {})
    month_data = payments.setdefault(month, {})
    if paid_on:
        month_data[card] = paid_on.isoformat() if isinstance(paid_on, date) else str(paid_on)
    else:
        month_data.pop(card, None)
        if not month_data:
            payments.pop(month, None)

    store: Store | None = hass.data.get(DATA_PAYMENTS_STORE)
    if store is not None:
        await store.async_save(payments)

    # Push the new `payments` attribute to the Energy History sensors.
    for coordinator in hass.data.get(DOMAIN, {}).values():
        coordinator.async_update_listeners()


# --------------------------------------------------------------------------- report
def collect_year(hass: HomeAssistant, year: int) -> dict[str, dict[int, float]]:
    """Per card: {month: kWh} for `year`, summed over all stations/accounts."""
    cards: dict[str, dict[int, float]] = {}
    for coordinator in hass.data.get(DOMAIN, {}).values():
        for history in coordinator.energy_history.values():
            for ydata in history:
                if ydata.get("year") != year:
                    continue
                for mdata in ydata.get("months", []):
                    for label, kwh in (mdata.get("cards") or {}).items():
                        if kwh:
                            per_month = cards.setdefault(label, {})
                            per_month[mdata["month"]] = per_month.get(mdata["month"], 0.0) + kwh
    return cards


def build_billing_rows(
    cards: dict[str, dict[int, float]],
    payments: dict[str, dict[str, str]],
    year: int,
    price: float,
    card_names: dict[str, str],
) -> list[dict]:
    """One block per card with its monthly rows and totals (pure, testable)."""
    def display(label: str) -> str:
        return card_names.get(label) or card_names.get(re.sub(r"^RFID\s+", "", label)) or label

    blocks = []
    for label in sorted(cards, key=lambda lbl: display(lbl).lower()):
        rows = []
        for month in sorted(cards[label]):
            kwh = round(cards[label][month], 2)
            amount = round(kwh * price, 2)
            paid = payments.get(f"{year:04d}-{month:02d}", {}).get(label)
            rows.append({"month": month, "kwh": kwh, "amount": amount, "paid_on": paid})
        total_kwh = round(sum(r["kwh"] for r in rows), 2)
        total = round(sum(r["amount"] for r in rows), 2)
        paid_sum = round(sum(r["amount"] for r in rows if r["paid_on"]), 2)
        blocks.append(
            {
                "label": label,
                "name": display(label),
                "rows": rows,
                "kwh": total_kwh,
                "amount": total,
                "paid": paid_sum,
                "open": round(total - paid_sum, 2),
            }
        )
    return blocks


async def async_create_billing_report(hass: HomeAssistant, data: dict) -> ServiceResponse:
    """Build the yearly billing PDF and return where to fetch it."""
    if not hass.data.get(DOMAIN):
        raise HomeAssistantError("No Elli Charger account is configured")
    year = data.get("year") or dt_util.now().year
    price = float(data.get("price_per_kwh") or 0)
    currency = data.get("currency") or "€"
    card_names = data.get("card_names") or {}
    title = data.get("title") or f"Ladestrom-Abrechnung {year}"

    blocks = build_billing_rows(
        collect_year(hass, year), async_get_payments(hass), year, price, card_names
    )
    target_dir = hass.config.path(REPORT_DIR)
    filename = f"abrechnung_{year}.pdf"
    path = await hass.async_add_executor_job(
        _render_pdf, target_dir, filename, title, year, price, currency, blocks
    )
    return {
        "url": REPORT_VIEW_URL.format(filename=filename),
        "file_path": path,
        "year": year,
        "total": round(sum(b["amount"] for b in blocks), 2),
        "open": round(sum(b["open"] for b in blocks), 2),
        "cards": len(blocks),
    }


def _fmt_num(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def _render_pdf(
    target_dir: str,
    filename: str,
    title: str,
    year: int,
    price: float,
    currency: str,
    blocks: list[dict],
) -> str:
    """Render the report with reportlab (executor - blocking I/O)."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            KeepTogether,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as err:  # pragma: no cover
        raise HomeAssistantError(
            "The 'reportlab' package is required for PDF reports. Restart Home "
            "Assistant so the integration's requirements get installed."
        ) from err

    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, filename)

    green = colors.HexColor("#2E7D32")
    light = colors.HexColor("#F1F8E9")
    grey = colors.HexColor("#757575")
    orange = colors.HexColor("#E65100")

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], alignment=0, fontSize=20, spaceAfter=4)
    sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=grey, fontSize=9)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=green, spaceBefore=14, spaceAfter=6)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=grey)

    def money(value: float) -> str:
        return f"{_fmt_num(value)} {currency}" if price > 0 else "–"

    now = dt_util.now()
    story = [
        Paragraph(title, h1),
        Paragraph(
            f"Abrechnungszeitraum: 01.01.{year} – "
            f"{'31.12.' + str(year) if year < now.year else now.strftime('%d.%m.%Y')}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;Preis: "
            f"{_fmt_num(price, 4).rstrip('0').rstrip(',') if price else '–'} {currency if price else ''}/kWh"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;Erstellt am {now.strftime('%d.%m.%Y %H:%M')}",
            sub,
        ),
        Spacer(1, 0.4 * cm),
    ]

    if not blocks:
        story.append(Paragraph(f"Für {year} liegen keine Ladevorgänge vor.", styles["Normal"]))
    else:
        # ---- overview
        story.append(Paragraph("Übersicht", h2))
        data = [["Mieter / Karte", "kWh", "Betrag", "bezahlt", "offen"]]
        for b in blocks:
            data.append([b["name"], _fmt_num(b["kwh"]), money(b["amount"]), money(b["paid"]), money(b["open"])])
        data.append([
            "Gesamt",
            _fmt_num(sum(b["kwh"] for b in blocks)),
            money(sum(b["amount"] for b in blocks)),
            money(sum(b["paid"] for b in blocks)),
            money(sum(b["open"] for b in blocks)),
        ])
        table = Table(data, colWidths=[6.2 * cm, 2.6 * cm, 2.8 * cm, 2.8 * cm, 2.8 * cm], repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), green),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, light]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
        ]
        for i, b in enumerate(blocks, start=1):
            if b["open"] > 0 and price > 0:
                style.append(("TEXTCOLOR", (4, i), (4, i), orange))
        table.setStyle(TableStyle(style))
        story.append(table)

        # ---- one block per tenant/card
        for b in blocks:
            rows = [["Monat", "kWh", "Betrag", "bezahlt am"]]
            for r in b["rows"]:
                rows.append([
                    f"{MONTHS_DE[r['month'] - 1]} {year}",
                    _fmt_num(r["kwh"]),
                    money(r["amount"]),
                    _fmt_date(r["paid_on"]) or ("offen" if price > 0 else ""),
                ])
            rows.append(["Summe", _fmt_num(b["kwh"]), money(b["amount"]), f"offen: {money(b['open'])}" if price > 0 else ""])
            t = Table(rows, colWidths=[5.5 * cm, 3 * cm, 3.5 * cm, 5.2 * cm], repeatRows=1)
            st = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F5E9")),
                ("TEXTCOLOR", (0, 0), (-1, 0), green),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, green),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#E0E0E0")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
            for i, r in enumerate(b["rows"], start=1):
                if not r["paid_on"] and price > 0:
                    st.append(("TEXTCOLOR", (3, i), (3, i), orange))
            t.setStyle(TableStyle(st))
            label_hint = f" <font size=8 color='#757575'>({b['label']})</font>" if b["name"] != b["label"] else ""
            story.append(KeepTogether([Paragraph(f"{b['name']}{label_hint}", h2), t]))

    story.append(Spacer(1, 0.8 * cm))
    story.append(Paragraph(
        "Verbrauchswerte laut Ladehistorie der Elli-Wallbox, Zuordnung über die RFID-Ladekarte. "
        "Beträge gerundet auf volle Cent.", small,
    ))

    def _footer(canvas, doc_) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(grey)
        canvas.drawString(2 * cm, 1.2 * cm, title)
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Seite {doc_.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        path, pagesize=A4, title=title, author="Elli Charger · Home Assistant",
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=2 * cm,
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path


class BillingReportView(HomeAssistantView):
    """Serve generated billing PDFs to authenticated users only."""

    url = REPORT_VIEW_URL
    name = "api:elli_charger:billing_report"
    requires_auth = True

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        """Return the requested PDF."""
        if not _SAFE_FILENAME.match(filename):
            return web.Response(status=400)
        hass = request.app[KEY_HASS]
        path = os.path.join(hass.config.path(REPORT_DIR), filename)
        if not await hass.async_add_executor_job(os.path.isfile, path):
            return web.Response(status=404)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
