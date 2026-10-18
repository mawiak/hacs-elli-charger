"""Automatic hand-off of Elli's OAuth callback to a waiting config flow.

Elli's Auth0 tenant only accepts the mobile app's redirect URI
(`com.elli.ios.emsp://login.elli.eco/ios/com.elli.ios.emsp/callback`). A
desktop browser cannot open that address, so without help the user has to
dig the callback out of the browser and paste it into Home Assistant.

The optional Windows login helper (served by `HelperDownloadView`, source in
`helper/install-elli-login-helper.ps1`) registers that URL scheme on the
user's PC. When the browser hands the callback over, the helper opens

    <home-assistant>/api/elli_charger/callback?url=<url-encoded callback>

and `CallbackView` forwards it to the config flow that is waiting for it.

Security: the view is unauthenticated (the browser opening it carries no
Home Assistant token), so it only acts on a callback whose OAuth `state`
matches a login that was started in Home Assistant moments ago. The state is
a 256-bit random, single-use value created by `elli-client`; anything else is
rejected without touching a flow. The authorization code itself is still
bound to the PKCE verifier held only by the flow.
"""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.http import KEY_HASS

from .const import CALLBACK_SCHEME, CALLBACK_VIEW_URL, DOMAIN, HELPER_VIEW_URL

_LOGGER = logging.getLogger(__name__)

DATA_PENDING = f"{DOMAIN}_pending_logins"
DATA_VIEWS = f"{DOMAIN}_auth_views_registered"

MAX_CALLBACK_LENGTH = 4096
HELPER_TEMPLATE = Path(__file__).parent / "helper" / "install-elli-login-helper.ps1"
HELPER_URL_PLACEHOLDER = "'@@HOME_ASSISTANT_URL@@'"
_SAFE_BASE_URL = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/-]*)?$")


@callback
def async_register_auth_views(hass: HomeAssistant) -> None:
    """Register the callback + helper download endpoints (idempotent)."""
    if hass.data.get(DATA_VIEWS):
        return
    hass.http.register_view(CallbackView())
    hass.http.register_view(HelperDownloadView())
    hass.data[DATA_VIEWS] = True


@callback
def async_track_login(hass: HomeAssistant, state: str, flow_id: str) -> None:
    """Remember which flow is waiting for the callback with this state."""
    hass.data.setdefault(DATA_PENDING, {})[state] = flow_id


@callback
def async_untrack_login(hass: HomeAssistant, state: str | None) -> None:
    """Forget a pending login (flow finished, aborted or restarted)."""
    if state:
        hass.data.get(DATA_PENDING, {}).pop(state, None)


def extract_state(callback_url: str) -> str | None:
    """Return the OAuth state of an Elli callback URL, or None if it isn't one."""
    if not callback_url or len(callback_url) > MAX_CALLBACK_LENGTH:
        return None
    try:
        parsed = urlparse(callback_url)
    except ValueError:
        return None
    if parsed.scheme.lower() != CALLBACK_SCHEME:
        return None
    states = parse_qs(parsed.query).get("state", [])
    return states[0] if len(states) == 1 and states[0] else None


def _page(title: str, message: str, status: int) -> web.Response:
    body = (
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:34rem;margin:15vh auto;"
        "padding:0 1rem;line-height:1.5;color:#222}h1{font-size:1.3rem}</style></head>"
        f"<body><h1>{html.escape(title)}</h1><p>{message}</p></body></html>"
    )
    return web.Response(text=body, content_type="text/html", status=status)


class CallbackView(HomeAssistantView):
    """Receive the Elli callback from the login helper and resume the flow."""

    url = CALLBACK_VIEW_URL
    name = "api:elli_charger:callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Handle the callback hand-off."""
        hass: HomeAssistant = request.app[KEY_HASS]
        callback_url = request.query.get("url", "")
        state = extract_state(callback_url)
        flow_id = hass.data.get(DATA_PENDING, {}).pop(state, None) if state else None

        if flow_id is None:
            return _page(
                "Elli: kein passender Anmeldevorgang",
                "Home Assistant wartet gerade auf keine Elli-Anmeldung mit diesem Link. "
                "Bitte die Einrichtung in Home Assistant neu starten und &bdquo;Automatisch "
                "anmelden&ldquo; w&auml;hlen.<br><br><i>No matching Elli login is waiting in "
                "Home Assistant. Please restart the setup there.</i>",
                404,
            )

        try:
            await hass.config_entries.flow.async_configure(
                flow_id=flow_id, user_input={"callback_url": callback_url}
            )
        except UnknownFlow:
            return _page(
                "Elli: Anmeldung abgelaufen",
                "Der Einrichtungsdialog in Home Assistant wurde inzwischen geschlossen. "
                "Bitte neu starten.<br><br><i>The setup dialog was closed meanwhile. "
                "Please start again.</i>",
                410,
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Error while completing the Elli login callback")
            return _page(
                "Elli: Fehler",
                "Beim Abschlie&szlig;en der Anmeldung ist ein Fehler aufgetreten. Details "
                "stehen im Home-Assistant-Protokoll.<br><br><i>Something went wrong, see the "
                "Home Assistant log.</i>",
                500,
            )

        return _page(
            "Elli: Anmeldung übertragen",
            "Die Anmeldung wurde an Home Assistant &uuml;bergeben. Dieses Fenster kann "
            "geschlossen werden &ndash; die Einrichtung wird im Home-Assistant-Dialog "
            "abgeschlossen.<br><br><i>Login handed over to Home Assistant. You can close "
            "this window.</i>",
            200,
        )


class HelperDownloadView(HomeAssistantView):
    """Serve the Windows login helper with this Home Assistant's URL filled in.

    Not secret (it only contains the URL the browser just used to reach
    Home Assistant), so it is served without authentication - a plain
    download link from the config flow dialog can't carry a token anyway.
    """

    url = HELPER_VIEW_URL
    name = "api:elli_charger:windows_login_helper"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the installer script."""
        hass: HomeAssistant = request.app[KEY_HASS]
        template = await hass.async_add_executor_job(
            HELPER_TEMPLATE.read_text, "utf-8"
        )
        base_url = f"{request.scheme}://{request.host}"
        if _SAFE_BASE_URL.match(base_url):
            template = template.replace(HELPER_URL_PLACEHOLDER, f"'{base_url}'", 1)
        # Otherwise leave the placeholder; the script then asks for the URL.
        return web.Response(
            body=template.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig"),
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename=install-elli-login-helper.ps1",
                "Cache-Control": "no-store",
            },
        )
