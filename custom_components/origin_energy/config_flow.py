"""Config flow for Origin Energy.

Asks for an imported browser session cookie rather than a password:
Origin's password endpoint is behind Kasada bot defence and cannot be
scripted. See docs/session-cookie.md.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    pick_service,
    pick_time_unit,
)
from .auth import (
    OriginEnergyAuthError,
    OriginEnergyBlocked,
    async_login_with_cookie,
    current_session_cookie,
)
from .const import (
    AUTH_DOMAIN,
    CONF_AGREEMENT_ID,
    CONF_MANUAL_STATISTIC_ID,
    CONF_RESET_STATISTICS,
    CONF_SESSION_COOKIE,
    CONF_TIME_UNIT,
    CONF_TIMEZONE,
    CONF_UPDATE_INTERVAL_HOURS,
    DEFAULT_TIMEZONE,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_UPDATE_INTERVAL_HOURS,
)
from .manual_floor import parse_statistic_id

_LOGGER = logging.getLogger(__name__)

STEP_USER_PLACEHOLDERS = {
    "jwks_url": f"https://{AUTH_DOMAIN}/.well-known/jwks.json",
    "cookie_origin": f"https://{AUTH_DOMAIN}",
}

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_SESSION_COOKIE): str})


class OriginEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Origin Energy."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OriginEnergyOptionsFlow:
        """Expose the Configure dialog on the integration entry."""
        return OriginEnergyOptionsFlow()

    def _configured_agreement_id(self) -> str | None:
        """Pin re-auth to the agreement this entry already tracks.

        Otherwise re-authenticating could silently move an entry onto a
        different meter, orphaning its statistics history.
        """
        if self.source != SOURCE_REAUTH:
            return None
        return self._get_reauth_entry().data.get(CONF_AGREEMENT_ID)

    async def _async_validate(
        self, cookie: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Exchange the cookie for tokens and read back the live service.

        Returns (entry_data, error_key) -- exactly one of which is set.
        """
        # A private session, because validating seeds a cookie into the jar
        # that must not leak into other integrations' requests.
        #
        # auto_cleanup=False + detach(): there is no config entry yet, so
        # automatic cleanup would only fire at Home Assistant shutdown and
        # the session would linger until then. Closing it instead is what
        # Home Assistant warns about -- detach() is the sanctioned way to
        # let go of one of its sessions.
        #
        # See coordinator.py: session default headers don't survive Home
        # Assistant, so the browser identity is applied per request.
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            tokens = await async_login_with_cookie(session, cookie)
            services = await OriginEnergyApiClient(
                session, tokens.access_token
            ).async_get_services("ELECTRICITY")

            # An account can list a closed agreement alongside the live one,
            # so services[0] is whichever the gateway happened to return
            # first rather than the one still supplying power.
            service = pick_service(services, self._configured_agreement_id())
            if service is None:
                return None, "no_services"
            timezone = (
                ((service.get("property") or {}).get("address") or {}).get(
                    "components", {}
                ).get("timezone")
                or DEFAULT_TIMEZONE
            )
            time_unit = pick_time_unit(service.get("availableUsageTimeUnits"))
            _LOGGER.debug("Origin Energy usage granularity: %s", time_unit)

            return {
                # Persist the cookie Auth0 handed back, not the one pasted in
                # -- it may already have been rolled forward.
                CONF_SESSION_COOKIE: current_session_cookie(session) or cookie,
                CONF_AGREEMENT_ID: service["agreementId"],
                CONF_TIMEZONE: timezone,
                CONF_TIME_UNIT: time_unit,
                "nmi": (service.get("meterPoint") or {}).get("identifier"),
            }, None
        except OriginEnergyBlocked:
            # The cookie may be perfectly good; telling the user to copy it
            # again would send them chasing the wrong thing.
            return None, "blocked"
        except OriginEnergyAuthError:
            return None, "invalid_auth"
        except OriginEnergyApiError:
            return None, "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error setting up Origin Energy")
            return None, "unknown"
        finally:
            session.detach()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data, error = await self._async_validate(
                user_input[CONF_SESSION_COOKIE].strip()
            )
            if error:
                errors["base"] = error
            else:
                assert data is not None
                await self.async_set_unique_id(data[CONF_AGREEMENT_ID])
                self._abort_if_unique_id_configured()
                nmi = data.pop("nmi", None)
                return self.async_create_entry(
                    title=f"Origin Energy ({nmi})" if nmi else "Origin Energy",
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders=STEP_USER_PLACEHOLDERS,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when the stored session cookie stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data, error = await self._async_validate(
                user_input[CONF_SESSION_COOKIE].strip()
            )
            if error:
                errors["base"] = error
            else:
                assert data is not None
                data.pop("nmi", None)
                # Signing in as a different customer would otherwise rewrite
                # this entry's agreement id, silently pointing years of
                # statistics at someone else's meter.
                await self.async_set_unique_id(data[CONF_AGREEMENT_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                # Keep the existing unique_id/entry so statistics history and
                # the Energy dashboard wiring survive the re-auth.
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class OriginEnergyOptionsFlow(OptionsFlow):
    """Let the poll interval be changed without editing the source.

    Origin publishes each day's usage in one batch at a time that moves
    around by hours (see DEFAULT_UPDATE_INTERVAL_HOURS), so the right
    interval depends on how promptly someone wants yesterday's data --
    which is a preference, not something the integration can derive.

    It also takes the id of a manual statistic: when set, Origin's data is
    only added for hours after that statistic's last row (see manual_floor.py).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current_manual = self.config_entry.options.get(CONF_MANUAL_STATISTIC_ID)

        if user_input is not None:
            current_manual = user_input.get(CONF_MANUAL_STATISTIC_ID)
            try:
                manual_id = parse_statistic_id(current_manual)
            except ValueError:
                errors[CONF_MANUAL_STATISTIC_ID] = "invalid_statistic_id"
            else:
                reset = bool(user_input.get(CONF_RESET_STATISTICS))
                coordinator = getattr(self.config_entry, "runtime_data", None)
                if reset and not manual_id:
                    errors[CONF_RESET_STATISTICS] = "reset_needs_statistic"
                elif reset and coordinator is None:
                    errors[CONF_RESET_STATISTICS] = "reset_not_loaded"
            if not errors:
                data: dict[str, Any] = {
                    # The number selector hands back a float; storing the
                    # int keeps the stored options readable and avoids
                    # 4.0 != 4 comparing unequal against the default.
                    CONF_UPDATE_INTERVAL_HOURS: int(
                        user_input[CONF_UPDATE_INTERVAL_HOURS]
                    )
                }
                # Left out entirely when blank: the options are replaced
                # wholesale by this call, so omitting the key is how it is
                # cleared.
                if manual_id:
                    data[CONF_MANUAL_STATISTIC_ID] = manual_id
                if reset:
                    # Saved first so the coordinator sees the new id, then
                    # the one-time clear runs. The tick-box itself is not
                    # stored, so it is off again next time.
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, options=data
                    )
                    try:
                        await coordinator.async_reset_for_manual_statistic(manual_id)
                    except ValueError:
                        errors[CONF_MANUAL_STATISTIC_ID] = "invalid_statistic_id"
                    except TimeoutError:
                        errors[CONF_RESET_STATISTICS] = "reset_timed_out"
                if not errors:
                    return self.async_create_entry(data=data)

        current = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_INTERVAL_HOURS, default=current
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_UPDATE_INTERVAL_HOURS,
                            max=MAX_UPDATE_INTERVAL_HOURS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="hours",
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_STATISTIC_ID,
                        description={"suggested_value": current_manual},
                    ): TextSelector(),
                    vol.Optional(CONF_RESET_STATISTICS, default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )
