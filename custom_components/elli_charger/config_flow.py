"""Config flow for Elli Charger integration.

Elli's Auth0-hosted login now requires an interactive Cloudflare Turnstile
challenge (confirmed against elli-client 1.5.0). Direct
username/password login from server-side code - what this flow used to do -
is therefore unreliable by design, not just flaky. `elli-client` >= 1.1.0
instead exposes a browser-based Authorization Code + PKCE flow:

1. `client.create_authorization()` builds an `authorization_url` (no network
   call) that the user opens in *any* real browser and logs into personally
   (that's what actually clears the Turnstile challenge).
2. Elli redirects to a custom mobile-app URL scheme
   (`com.elli.ios.emsp://.../callback?code=...&state=...`) that a browser on
   a PC can't open. Either the optional Windows login helper forwards it to
   Home Assistant automatically (see `auth_callback.py`), or the user copies
   that URL out of the browser and pastes it back into this config flow.
3. `client.exchange_callback(callback_url, authorization)` validates the
   `state` and exchanges the `code` for tokens.

We then store only the resulting (opaque, revocable) refresh token - never
the account password.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from elli_client import (
    AuthorizationSession,
    ElliAPIClient,
    InvalidOAuthCallback,
    TokenExchangeError,
    TokenResponse,
)

from .auth_callback import (
    async_register_auth_views,
    async_track_login,
    async_untrack_login,
)
from .const import (
    CONF_ACCOUNT_LABEL,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SOLAR_ENTITY,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    HELPER_VIEW_URL,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

STEP_CALLBACK_SCHEMA = vol.Schema({vol.Required("callback_url"): str})


def _create_authorization() -> AuthorizationSession:
    """Build a fresh authorization session (blocking client construction)."""
    client = ElliAPIClient()
    try:
        return client.create_authorization()
    finally:
        client.close()


def _exchange_callback(
    callback_url: str, authorization: AuthorizationSession
) -> TokenResponse:
    """Exchange the pasted callback URL for tokens (blocking network call)."""
    client = ElliAPIClient()
    try:
        return client.exchange_callback(callback_url, authorization)
    finally:
        client.close()


def _decode_jwt_claim(token: str | None, claim: str) -> str | None:
    """Best-effort, UNVERIFIED read of one claim from a JWT.

    Used only to get a human-friendly label (e.g. the account email) for the
    config entry title/unique_id. Never used for any security decision - the
    token's signature is intentionally not checked here.
    """
    if not token or token.count(".") != 2:
        return None
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        value = payload.get(claim)
        return str(value) if value else None
    except Exception:  # pylint: disable=broad-except
        return None


def _account_label(tokens: TokenResponse) -> str:
    """Derive a display label for the account from the tokens we got back."""
    for token, claim in (
        (tokens.id_token, "email"),
        (tokens.access_token, "email"),
        (tokens.id_token, "sub"),
        (tokens.access_token, "sub"),
    ):
        if label := _decode_jwt_claim(token, claim):
            return label
    # Nothing decodable - fall back to a short random, still-stable label.
    return f"account-{secrets.token_hex(3)}"


class ElliConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Elli Charger.

    Two ways to finish the browser login (v1.5.0):

    * **Automatic** (`login_auto`): an external step. Home Assistant opens the
      Elli login; the optional Windows login helper hands the
      `com.elli.ios.emsp://...` callback to `auth_callback.CallbackView`,
      which resumes this flow - no copy & paste.
    * **Manual** (`login_manual`): the original paste-the-link form, as a
      fallback for devices without the helper.
    """

    # Deliberately left at 1: the auth data shape changed (refresh token
    # instead of email/password), but that's handled at runtime in
    # `async_setup_entry` (which raises ConfigEntryAuthFailed for old
    # entries, triggering the reauth flow below) rather than via Home
    # Assistant's entry-version migration machinery, since there is no
    # actual field-by-field data to migrate - only a full new login.
    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reauth_entry: config_entries.ConfigEntry | None = None
        self._authorization: AuthorizationSession | None = None
        self._tracked_state: str | None = None
        self._tokens: TokenResponse | None = None
        self._login_error: str | None = None

    # ------------------------------------------------------------- helpers
    def _menu_placeholders(self) -> dict[str, str]:
        return {"helper_url": HELPER_VIEW_URL}

    async def _async_new_authorization(self) -> AuthorizationSession:
        """Create a fresh (single-use) authorization session."""
        self._untrack()
        self._authorization = await self.hass.async_add_executor_job(
            _create_authorization
        )
        return self._authorization

    def _untrack(self) -> None:
        async_untrack_login(self.hass, self._tracked_state)
        self._tracked_state = None

    async def _async_new_authorization_form(
        self, step_id: str, errors: dict[str, str]
    ) -> FlowResult:
        """(Re)generate the authorization link and show the paste-back form.

        A fresh link is generated every time this is shown (including after
        an error) because Auth0's authorization codes are short-lived and
        single-use.
        """
        authorization = await self._async_new_authorization()
        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP_CALLBACK_SCHEMA,
            description_placeholders={
                "authorize_url": authorization.authorization_url,
                **self._menu_placeholders(),
            },
            errors=errors,
        )

    async def _async_exchange(self, callback_url: str) -> TokenResponse:
        """Exchange the pasted/forwarded callback URL, translating errors."""
        assert self._authorization is not None
        try:
            return await self.hass.async_add_executor_job(
                _exchange_callback, callback_url, self._authorization
            )
        except InvalidOAuthCallback as err:
            _LOGGER.error("Invalid Elli OAuth callback: %s", err)
            raise InvalidCallback from err
        except TokenExchangeError as err:
            _LOGGER.error("Elli token exchange failed: %s", err)
            raise CannotConnect from err

    async def _async_try_exchange(self, callback_url: str) -> str | None:
        """Exchange and remember the tokens; return an error key or None."""
        try:
            self._tokens = await self._async_exchange(callback_url)
        except InvalidCallback:
            return "invalid_callback"
        except CannotConnect:
            return "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception during Elli login")
            return "unknown"
        return None

    async def _async_finish_login(self) -> FlowResult:
        """Create the entry, or update it when re-authenticating."""
        assert self._tokens is not None
        tokens = self._tokens
        label = _account_label(tokens)

        if self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                data={
                    **self._reauth_entry.data,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_ACCOUNT_LABEL: label,
                },
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(label)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Elli ({label})",
            data={
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_ACCOUNT_LABEL: label,
            },
        )

    @callback
    def async_remove(self) -> None:
        """Clean up a still-pending automatic login when the flow goes away."""
        self._untrack()

    # --------------------------------------------------------------- steps
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user choose between automatic and manual login."""
        async_register_auth_views(self.hass)
        return self.async_show_menu(
            step_id="user",
            menu_options=["login_auto", "login_manual"],
            description_placeholders=self._menu_placeholders(),
        )

    async def async_step_login_auto(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Open the Elli login and wait for the helper to forward the callback."""
        if user_input is None:
            async_register_auth_views(self.hass)
            authorization = await self._async_new_authorization()
            async_track_login(self.hass, authorization.state, self.flow_id)
            self._tracked_state = authorization.state
            return self.async_external_step(
                step_id="login_auto",
                url=authorization.authorization_url,
                description_placeholders=self._menu_placeholders(),
            )

        # Called by auth_callback.CallbackView with the forwarded callback.
        self._untrack()
        self._login_error = await self._async_try_exchange(user_input["callback_url"])
        if self._login_error:
            return self.async_external_step_done(next_step_id="login_failed")
        return self.async_external_step_done(next_step_id="login_done")

    async def async_step_login_done(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish after a successful automatic login."""
        return await self._async_finish_login()

    async def async_step_login_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Automatic login failed - fall back to the manual form with the error."""
        error = self._login_error or "unknown"
        self._login_error = None
        return await self._async_new_authorization_form("login_manual", {"base": error})

    async def async_step_login_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the login link, then accept the pasted callback URL."""
        errors: dict[str, str] = {}
        if user_input is not None and self._authorization is not None:
            error = await self._async_try_exchange(user_input["callback_url"])
            if error is None:
                return await self._async_finish_login()
            errors["base"] = error

        return await self._async_new_authorization_form("login_manual", errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle re-authentication triggered by ConfigEntryAuthFailed.

        This happens when Elli/Auth0 rejects our *refresh token* (revoked,
        expired, or the account requires a fresh interactive login) - Home
        Assistant surfaces a "Reauthenticate" notification that walks the
        user through the same login again, rather than silently failing
        forever.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask how to redo the browser login for reauth."""
        async_register_auth_views(self.hass)
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=["login_auto", "login_manual"],
            description_placeholders=self._menu_placeholders(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ElliOptionsFlow:
        """Create the options flow (Improvement #7: configurable polling)."""
        return ElliOptionsFlow()


class ElliOptionsFlow(config_entries.OptionsFlow):
    """Handle Elli Charger options (currently: the polling interval).

    `self.config_entry` is provided by Home Assistant (2024.11+); assigning it
    manually raises an AttributeError on current versions.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        current_solar = self.config_entry.options.get(CONF_SOLAR_ENTITY)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=current
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_MINUTES,
                            max=MAX_SCAN_INTERVAL_MINUTES,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                    # v1.6.0: optional PV energy sensor for the solar-share
                    # estimate (suggested_value so it can be cleared again).
                    vol.Optional(
                        CONF_SOLAR_ENTITY,
                        description={"suggested_value": current_solar},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor", device_class="energy"
                        )
                    ),
                }
            ),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidCallback(HomeAssistantError):
    """Error to indicate the pasted callback URL was rejected."""
