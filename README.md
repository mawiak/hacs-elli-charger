# Elli Charger Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub Release](https://img.shields.io/github/release/marcszy91/hacs-elli-charger.svg)](https://github.com/marcszy91/hacs-elli-charger/releases)

Home Assistant integration for Elli charging stations (wallboxes). Monitor your charging sessions and station status directly in Home Assistant.

## Features

- Real-time charging session monitoring (status, energy, power)
- Energy totals for this month, last month and this year
- The last 10 charges, like the list in the Elli app
- All-time charging history (year → month → RFID card)
- Bundled dashboard card with a Grafana-style year/month overview, live charging header, recent charges, year projection and optional solar share
- Monthly PDF report per RFID card, optionally sent by e-mail
- Tenant billing: record when each month was paid per RFID card and print a yearly billing report (PDF)
- All sensors grouped under one device per wallbox
- Browser-based login (no password stored), automatic token refresh and re-authentication
- Configurable polling interval

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add the repository URL: `https://github.com/marcszy91/hacs-elli-charger`
6. Select category: "Integration"
7. Click "Add"
8. Find "Elli Charger" in the integration list
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the latest release from [GitHub](https://github.com/marcszy91/hacs-elli-charger/releases)
2. Extract the files to your `custom_components` directory:
   ```
   custom_components/elli_charger/
   ```
3. Restart Home Assistant

## Configuration

Elli's login requires a security check that only works in a real browser, and afterwards Elli only redirects to its own mobile app (`com.elli.ios.emsp://…`) – an address a desktop browser can't open. So you log in in your browser and hand the result to Home Assistant. There are two ways:

### Option A: automatic login (Windows, recommended)

One-time setup on the PC you log in from:

1. Go to **Settings** → **Devices & Services** → **+ Add Integration** → "Elli Charger".
2. In the dialog, click **download helper**. Home Assistant serves `install-elli-login-helper.ps1` with its own address already filled in.
3. Right-click the file → **Run with PowerShell**. It registers the `com.elli.ios.emsp://` address for your Windows user (no admin rights, nothing else is changed).

Then, every time you (re-)connect:

1. Choose **Log in automatically**. A browser tab with the Elli login opens.
2. Log in and complete the security check.
3. When the browser asks whether to open the link with *Windows PowerShell*, allow it (you can tick "always allow" for login.elli.eco).
4. A confirmation page opens and the Home Assistant dialog finishes by itself.

How it works: the helper forwards the callback to `/api/elli_charger/callback` on your Home Assistant. That endpoint only accepts a callback whose one-time OAuth `state` matches a login you just started in Home Assistant; everything else is rejected. The authorization code is additionally bound to the PKCE verifier that never leaves Home Assistant.

Uninstall the helper: `powershell -ExecutionPolicy Bypass -File install-elli-login-helper.ps1 -Uninstall`. The script is also in this repository: [`custom_components/elli_charger/helper/install-elli-login-helper.ps1`](custom_components/elli_charger/helper/install-elli-login-helper.ps1) (it asks for your Home Assistant URL when run from there).

### Option B: manual login (any device)

1. Choose **Log in and paste the link manually** and open the **Log in to Elli** link.
2. Log in. Your browser then tries to open a link starting with `com.elli.ios.emsp://…` and silently fails – that's expected.
3. Get that **complete** link, e.g. in Chrome/Edge: press **F12** → **Network**, tick **Preserve log** *before* logging in, then filter for `emsp` and copy the **Location** response header of the last request.
4. Paste it into the dialog and submit.

Either way, only a revocable refresh token is stored, never your password. If it ever becomes invalid, Home Assistant shows a **Reauthenticate** prompt that offers the same two options.

### Options

Under **Settings** → **Devices & Services** → **Elli Charger** → **Configure** you can change:

- **Polling interval** (1–60 minutes, default 5) – applies immediately.
- **PV / solar energy sensor** (optional) – a cumulative energy sensor of your PV system (e.g. the *Solar production* sensor from the Energy Dashboard, device class `energy`). If set, the integration estimates how much of each charge could have come from solar, using Home Assistant's long-term statistics. The integration reloads when this changes.

## Sensors

All sensors of a wallbox belong to one device named after the station. Entity IDs follow the device name, e.g. `sensor.<station>_session_energy`.

| Sensor | Unit | Description |
|---|---|---|
| Status | – | `idle` / `connected` / `charging` (translated in the UI). Attributes: station ID, name, serial number, model, firmware version |
| Last Session | – | Charging state of the current/last session. Attributes: energy, power, RFID card, authentication method, start/end time, … |
| Session Energy | kWh | Energy of the current/last session; keeps its value between sessions (Energy Dashboard ready) |
| Session Power | W | Current charging power, `0` when not charging |
| Energy This Month | kWh | Charged so far this calendar month |
| Energy Last Month | kWh | Charged in the previous calendar month |
| Energy This Year | kWh | Charged so far this calendar year |
| Energy History | kWh | All-time total; attributes `years` (year → month → card breakdown), `recent_charges` (last charges with duration and average power), `live` (current session) – used by the dashboard card |
| Last charges 1–10 | kWh | One sensor per recent charge (1 = most recent). Name = start date/time, state = energy. Attributes: card, start/end time, label |

The month/year, history and last-charges sensors are calculated from Elli's charging history and refreshed about once an hour, so they may take a few minutes to fill after a restart.

## Dashboard card

The integration ships its own card, `custom:elli-history-card`. It is registered automatically – you do **not** need to add a resource in HACS or under *Dashboards → Resources*.

**Add it via the UI:** open a dashboard → **Edit** → **Add card** → search for **"Elli Ladehistorie"** (or choose *Manual* and paste the YAML below).

**Minimal configuration** (the Energy History sensor is detected automatically):

```yaml
type: custom:elli-history-card
```

**Example** (costs at 0.38 €/kWh, RFID card shown with a name):

```yaml
type: custom:elli-history-card
title: Wallbox · Ladehistorie
price_per_kwh: 0.38
card_names:
  "RFID xxxx": Basti
```

Replace `xxxx` with your card's serial number exactly as it appears in the card (or in the `card` attribute of the *Last charges* sensors). Without `card_names` the card shows the raw label, e.g. `RFID 04A1B2C3`.

**All options:**

| Option | Default | Description |
|---|---|---|
| `entity` | auto-detected | The *Energy History* sensor, e.g. `sensor.my_wallbox_energy_history` |
| `title` | `Wallbox · Ladehistorie` | Card title |
| `price_per_kwh` | – | Price per kWh; adds cost figures to the stats, table and tooltips |
| `currency` | `€` | Currency symbol for the costs |
| `compare` | `true` | Show the previous year as ghost bars in the chart |
| `theme` | `dark` | `dark` (Grafana look) or `ha` (follow your Home Assistant theme) |
| `card_names` | – | Map of RFID card labels to friendly names |
| `show_live` | `true` | Live header while a car is connected/charging: status, power, kWh and duration of the current session |
| `show_recent` | `true` | Table of the last charges (start, duration, kWh, Ø kW, card, cost, solar share) |
| `recent_count` | `10` | Number of rows in the recent charges table (1–10) |
| `show_billing` | `true` | "Abrechnung" section for tenant billing: amount and a *paid on* date per month and RFID card, open amount and a PDF report button (amounts need `price_per_kwh`) |
| `recent_expanded` | `false` | Start with the recent charges unfolded. The table folds like the years: a summary row (count, duration, kWh, cost, solar) that expands to the single charges; the last state is remembered per browser |
| `show_projection` | `true` | Year-end projection (linear extrapolation, shown from day 14 of the year) |
| `show_solar` | `true` | Solar share in stats, chart, tooltips and tables – only if a PV sensor is set in the integration options |

The card shows year totals, average per month, number of charges, year-over-year change, the projected year total, a monthly bar chart (hover for details per RFID card) and an expandable year/month table.

**Solar share.** Once a PV sensor is configured (see [Options](#options)), the card adds a yellow overlay to the monthly bars and a "Sonne" column to the tables. The value is an **upper-bound estimate**: each charge is spread evenly over its duration and capped, hour by hour, at what the PV system produced. Household consumption and battery storage are not taken into account, so the real solar share can be lower. Periods without PV statistics show `–`.

> If the card doesn't appear right after updating, reload the browser (Ctrl+F5) once. After a restart the history is loaded in the background; until it is there, the card shows a loading view with a countdown to the next data request (retried every 3 minutes).

## Tenant billing (payment dates + yearly PDF)

If you pass the charging costs on to tenants (one RFID card per tenant), the card shows a section **"Abrechnung <year>"** below the year/month table:

- one row per month and card with kWh, amount (`price_per_kwh`) and a **"Bezahlt am"** date field,
- ✓ = paid, ● = open (the running month is not counted as open),
- the open amount of the year in the header, and a **"📄 PDF-Bericht"** button.

Payment dates are stored in Home Assistant (`.storage/elli_charger.payments`), survive restarts and are exposed as the `payments` attribute of the Energy History sensor. Clearing a date removes it.

The PDF (A4) contains an overview per tenant (kWh, amount, paid, open) and one table per tenant with month, kWh, amount and payment date. It is saved to `/config/elli_charger_reports/` – **not** under `www` – and served only to logged-in users via `/api/elli_charger/report/<file>`, because it contains names and amounts.

Both features are also available as services:

```yaml
action: elli_charger.set_payment
data:
  month: "2026-09"
  card: "RFID 04A1B2C3"
  paid_on: "2026-10-05"   # empty = remove
```

```yaml
action: elli_charger.create_billing_report
data:
  year: 2026              # optional, defaults to the current year
  price_per_kwh: 0.38
  card_names:
    "RFID 04A1B2C3": Tenant ground floor
response_variable: report   # report.url, report.total, report.open
```

## Monthly report (PDF)

The service `elli_charger.send_monthly_report` creates a PDF with the kWh charged per RFID card for one month and stores it in `/config/www/elli_charger/`. Optionally it sends the PDF through a `notify` service (the SMTP notify platform attaches it to the e-mail).

```yaml
action: elli_charger.send_monthly_report
data:
  month: "2026-09"                 # optional, defaults to last month
  notify_service: notify.email_me  # optional, leave empty to only create the PDF
  target: "you@example.com"        # optional, forwarded to the notify service
```

Example automation – send last month's report on the 1st of every month:

```yaml
automation:
  - alias: "Elli monthly report"
    triggers:
      - trigger: time
        at: "08:00:00"
    conditions:
      - condition: template
        value_template: "{{ now().day == 1 }}"
    actions:
      - action: elli_charger.send_monthly_report
        data:
          notify_service: notify.email_me
```

## Usage Examples

Replace `my_wallbox` with the entity ID prefix of your wallbox.

### Automation: Notify when charging complete

```yaml
automation:
  - alias: "Notify when EV charging complete"
    trigger:
      - platform: numeric_state
        entity_id: sensor.my_wallbox_session_power
        below: 100
        for:
          minutes: 5
    condition:
      - condition: numeric_state
        entity_id: sensor.my_wallbox_session_energy
        above: 5
    action:
      - service: notify.mobile_app
        data:
          message: "EV charging complete! {{ states('sensor.my_wallbox_session_energy') }} kWh charged"
```

### Energy Dashboard

Add the energy sensor to your Home Assistant Energy Dashboard:

1. Go to **Settings** → **Dashboards** → **Energy**
2. Under "Electricity grid" → "Add consumption"
3. Select the **Session Energy** sensor of your wallbox (e.g. `sensor.my_wallbox_session_energy`)

## API Client

This integration uses the [elli-client](https://pypi.org/project/elli-client/) Python package for communication with the Elli API.

## Support

- [Report Issues](https://github.com/marcszy91/hacs-elli-charger/issues)
- [Feature Requests](https://github.com/marcszy91/hacs-elli-charger/issues)

## License

MIT License - see [LICENSE](LICENSE) file for details

## Disclaimer

This integration is not officially supported by Elli or Volkswagen Group Charging GmbH. Use at your own risk.
