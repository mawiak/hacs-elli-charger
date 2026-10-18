## 1.0.0 (2025-11-14)


### Features

* initial release of Elli Charger Home Assistant integration ([f86837a](https://github.com/marcszy91/hacs-elli-charger/commit/f86837aa4c1a9708f9a1a6a72cdda632c855c8ee))

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Large feature and robustness update. Existing entity IDs are kept (all `unique_id`s are unchanged).

### ⚠️ Breaking changes
- **New login method.** Elli's login now requires an interactive Cloudflare Turnstile check, so the old email/password login no longer works from Home Assistant. Setup now uses a browser-based login (Authorization Code + PKCE): open the link, log in, paste the resulting `com.elli.ios.emsp://…` link back into Home Assistant. Only a revocable refresh token is stored – never your password. Existing entries are asked to re-authenticate.
- **Status sensor states are now lowercase enum values** (`idle`, `connected`, `charging`) and are translated in the UI. Automations that compare against `Idle` / `Connected` / `Charging` must be updated.
- Minimum `elli-client` version is now `1.5.0`; Home Assistant `2024.11` or newer is required.

### Added
- **Automatic login without copy & paste (Windows).** The setup and re-authentication dialogs now offer *Log in automatically*: a small optional helper (downloadable from the dialog, with your Home Assistant URL pre-filled) registers the `com.elli.ios.emsp://` address on your PC and forwards Elli's login callback to Home Assistant, which then finishes the setup by itself. The callback endpoint only accepts the one-time `state` of a login that is currently waiting. The manual paste-the-link login remains available for all other devices.
- **Session Power sensor** (W) – documented in the README since 1.0.0 but never actually created.
- **Energy This Month / Energy Last Month / Energy This Year** sensors (kWh), calculated from Elli's charging history.
- **Last 10 charges** – ten sensors (rank 1 = most recent). The entity name shows the start date/time (e.g. `01.10.26 18:57`), the state is the charged energy in kWh; card/RFID, start/end time and a full label are attributes.
- **Energy History sensor** – all-time total in kWh with a year → month → RFID-card breakdown in its attributes (excluded from the recorder to keep the database small).
- **Dashboard card `custom:elli-history-card`** – Grafana-style year/month overview with year-over-year comparison, per-card breakdown and optional cost calculation. It ships with the integration and is loaded automatically (no extra resource needed).
- **Dashboard card: live header** – while a car is connected or charging, the card shows status, current power, energy and duration of the running session (updates every minute).
- **Dashboard card: recent charges table** – start, duration, kWh, average kW, card, cost and solar share of the last charges (`show_recent`, `recent_count`). Collapsible like the year rows: a summary row expands to the single charges (`recent_expanded`, state remembered per browser).
- **Dashboard card: year projection** – linear extrapolation of the current year's total, shown from day 14 (`show_projection`).
- **Solar share (optional)** – pick a PV energy sensor in the integration options; the charge energy covered by PV is estimated from Home Assistant's long-term statistics (upper bound) and shown as overlay, tooltip, stat and table column in the card (`show_solar`). Energy History gains `recent_charges` and `live` attributes.
- **Tenant billing** – the card gets an "Abrechnung" section with the amount and a *paid on* date per month and RFID card, the open amount and a **PDF billing report** for the year (overview per tenant + monthly table with payment dates). New services `elli_charger.set_payment` and `elli_charger.create_billing_report` (returns the report URL). Payment dates are stored in Home Assistant's storage; the PDF is served to logged-in users only (`show_billing`).
- **Service `elli_charger.send_monthly_report`** – creates a PDF with kWh per RFID card for a month and can send it through any `notify` service (e.g. SMTP e-mail).
- **Options flow** – polling interval configurable in the UI (1–60 min, default 5), applied without restart.
- **Re-authentication flow** – if the refresh token is revoked or expires, Home Assistant shows a "Reauthenticate" prompt instead of failing silently.
- German and English translations for the new setup, options, status states and service.
- README: step-by-step login guide (automatic and manual, incl. how to get the callback link via the browser's developer tools) and a dashboard card example with all options.

### Changed
- All sensors of a wallbox are grouped under **one device** (manufacturer, model, firmware version).
- Entity names now use Home Assistant's `has_entity_name` convention (device name + short sensor name).
- Access tokens are refreshed **proactively** before they expire; a 401 triggers exactly one refresh-and-retry.
- Firmware info is polled only every 6th cycle and history statistics roughly once an hour, reducing API calls.

### Fixed
- **Dashboard card no longer shows a "configuration error" after a restart.** The card is now registered when Home Assistant loads the integration, before any Elli request, instead of only after the first (slow) history download. While data is still missing, the card shows a loading view with a countdown to the next data request instead of an error, and rendering problems are caught.
- **History no longer fails on incomplete records.** Elli sometimes returns charging records without `total_energy_wh` (e.g. aborted sessions); `elli-client` then rejected the whole page and the integration lost the entire year (`validation errors for ChargingRecordsResponse`). Records are now validated one by one; ones without energy are skipped. This also fixes the monthly PDF report for such months.
- **Charging history is loaded in the background and retried every 3 minutes** until it is complete (up to 5 times, then hourly; previously only on the hourly statistics cycle), so a temporary Elli API error no longer leaves the history empty for up to an hour. Setup itself no longer waits for the history download.
- **Options dialog (polling interval) crashed on current Home Assistant versions** (`AttributeError: property 'config_entry' … has no setter`); the options flow no longer assigns `config_entry` itself.
- **Session Energy** no longer drops to `unknown` between sessions or after a restart (last value is restored), which avoids gaps in the Energy Dashboard.
- Rotated refresh tokens are persisted, so the integration doesn't break after a Home Assistant restart.
- The API client is created in the executor, fixing Home Assistant's "blocking call inside the event loop" warning.
- Transient token-endpoint errors now surface as a normal failed update instead of an unhandled exception.

## [1.0.0] - 2025-11-13

### Added
- Initial release of Elli Charger HACS integration
- authentication with Elli Account
- Station-based sensor architecture supporting multiple wallboxes
- Wallbox status sensor (Idle/Connected/Charging)
- Last session sensor with comprehensive attributes
- Session energy sensor optimized for Home Assistant Energy Dashboard
- Firmware version display in wallbox attributes
- Automatic session tracking with energy consumption
- RFID card tracking for billing
- Support for momentary power monitoring
- Lifecycle and charging state tracking

### Features
- Three sensors per charging station
- Energy consumption tracking in kWh
- Real-time power monitoring in Watts
- Session state and lifecycle tracking
- RFID card identification
- Authentication method tracking
- Firmware version monitoring
- Automatic token refresh
