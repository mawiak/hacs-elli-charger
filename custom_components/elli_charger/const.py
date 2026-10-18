"""Constants for the Elli Charger integration."""

DOMAIN = "elli_charger"

# Default values
DEFAULT_NAME = "Elli Charger"

# --- Auth (browser PKCE login, elli-client >= 1.5.0) ---
# Elli's Auth0 login now requires an interactive Cloudflare Turnstile
# challenge, so direct username/password login is no longer possible from
# server-side code. We store only an opaque, revocable refresh token -
# never a password.
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCOUNT_LABEL = "account_label"

# --- Polling configuration (Improvement #7: configurable scan interval) ---
CONF_SCAN_INTERVAL = "scan_interval"
# Optional PV energy sensor used to estimate the solar share of each charge.
CONF_SOLAR_ENTITY = "solar_entity"
DEFAULT_SCAN_INTERVAL_MINUTES = 5
MIN_SCAN_INTERVAL_MINUTES = 1
MAX_SCAN_INTERVAL_MINUTES = 60

# How many regular refresh cycles pass between firmware-info polls.
# Firmware rarely changes, so we don't need to hit that endpoint every cycle
# (Improvement #6: reduce unnecessary API calls).
FIRMWARE_POLL_EVERY_N_CYCLES = 6

# How many regular refresh cycles pass between recomputing the "energy this
# month / last month / this year" sensors. Unlike the cheap "current
# session" endpoint the other sensors use, these read Elli's historical
# *records* endpoint (potentially several paginated requests per station),
# so they are refreshed far less often than the default 5-minute poll -
# with the default scan interval this is roughly once an hour.
STATS_POLL_EVERY_N_CYCLES = 12

# Safety margin (seconds) subtracted from the OAuth2 access-token lifetime so
# we proactively refresh *before* it actually expires instead of waiting for
# a 401 (Improvement #4: token-expiry tracking).
TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60

# Storage keys used on the coordinator/runtime data.
DATA_SESSIONS = "sessions"
DATA_STATIONS = "stations"

# Report defaults (monthly kWh-per-card report / bonus feature).
DEFAULT_REPORT_NOTIFY_SERVICE = "notify.notify"
# Safety cap on how many pages of historical charging records we'll fetch
# for a single monthly report, so a runaway API response can't hang forever.
REPORT_MAX_RECORD_PAGES = 20
REPORT_RECORDS_PAGE_SIZE = 100

# --- Energy history (year/month overview + Grafana-style dashboard card) ---
# How many *completed* years before the current one we look back at most.
# Loading stops earlier at the first year without any charging records.
HISTORY_MAX_YEARS_BACK = 10
# Until the history is complete (after a restart or an Elli API hiccup) it is
# retried this often instead of only on the hourly stats cycle.
HISTORY_RETRY_SECONDS = 180
# ...but only this many times in a row, then the hourly cycle takes over.
HISTORY_MAX_QUICK_RETRIES = 5
# The dashboard card ships with the integration and is served from here.
HISTORY_CARD_URL = "/elli_charger/elli-history-card.js"
HISTORY_CARD_VERSION = "1.6.4"

# --- Automatic login callback (Windows protocol-handler helper) ---
# Elli only accepts its own mobile-app redirect URI, which a desktop browser
# cannot open. A tiny, optional helper on the user's PC registers that URL
# scheme and forwards the callback to this (unauthenticated, state-checked)
# endpoint, which completes the waiting config flow automatically.
CALLBACK_SCHEME = "com.elli.ios.emsp"
CALLBACK_VIEW_URL = "/api/elli_charger/callback"
HELPER_VIEW_URL = "/api/elli_charger/windows-login-helper.ps1"
