"""Config flow for the Mazda 6e integration."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import Mazda6eClient, MazdaApiError, MazdaAuthError, MazdaConnectionError
from .const import (
    CONF_CONTROL_PRIVATE_KEY,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    REGION_ASIA,
    REGION_EUROPE,
)
from .crypto import generate_key_pair

_LOGGER = logging.getLogger(__name__)

CODE_SCHEMA = vol.Schema({vol.Required("code"): str})


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
            ),
            vol.Required(CONF_REGION, default=defaults.get(CONF_REGION, REGION_EUROPE)): SelectSelector(
                SelectSelectorConfig(
                    options=[REGION_EUROPE, REGION_ASIA],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_REGION,
                )
            ),
        }
    )


class Mazda6eConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in like the app does, including e-mail device verification."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: Mazda6eClient | None = None
        self._email: str = ""
        self._region: str = REGION_EUROPE
        self._device_id: str | None = None
        self._private_key: str | None = None
        self._needs_verification = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._region = user_input[CONF_REGION]
            if self.source != "reauth":
                await self.async_set_unique_id(self._email.lower())
                self._abort_if_unique_id_configured()

            errors = await self._async_login(user_input[CONF_PASSWORD])
            if not errors:
                if self._needs_verification:
                    return await self.async_step_verify()
                return await self._async_finish()

        defaults = user_input or {CONF_EMAIL: self._email, CONF_REGION: self._region}
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(defaults), errors=errors
        )

    async def _async_login(self, password: str) -> dict[str, str]:
        # Keep the device id across re-authentications so Mazda does not
        # see a new device (and ask for another e-mail code) every time.
        if self._device_id is None:
            self._device_id = str(uuid.uuid4())
        public_key, self._private_key = await self.hass.async_add_executor_job(generate_key_pair)
        self._client = Mazda6eClient(
            async_get_clientsession(self.hass), self._region, self._device_id
        )
        try:
            self._needs_verification = await self._client.login(self._email, password, public_key)
            if self._needs_verification:
                await self._client.request_device_code(self._email)
        except MazdaAuthError as err:
            _LOGGER.warning("Mazda login rejected (code %s)", err.code)
            return {"base": "invalid_auth"}
        except MazdaConnectionError:
            return {"base": "cannot_connect"}
        except MazdaApiError as err:
            _LOGGER.warning("Mazda login failed (code %s)", err.code)
            return {"base": "unknown"}
        except Exception:
            _LOGGER.exception("Unexpected error during Mazda login")
            return {"base": "unknown"}
        return {}

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client is not None
            try:
                await self._client.verify_device_code(self._email, user_input["code"])
            except MazdaConnectionError:
                errors["base"] = "cannot_connect"
            except (MazdaApiError, MazdaAuthError):
                errors["base"] = "invalid_code"
            else:
                return await self._async_finish()

        return self.async_show_form(
            step_id="verify",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def _async_finish(self) -> ConfigFlowResult:
        assert self._client is not None
        data = {
            CONF_EMAIL: self._email,
            CONF_REGION: self._region,
            CONF_DEVICE_ID: self._device_id,
            CONF_TOKEN: self._client.token,
            CONF_REFRESH_TOKEN: self._client.refresh_token,
            CONF_CONTROL_PRIVATE_KEY: self._private_key,
        }
        if self.source == "reauth":
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        return self.async_create_entry(title=self._email, data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._email = entry_data.get(CONF_EMAIL, "")
        self._region = entry_data.get(CONF_REGION, REGION_EUROPE)
        self._device_id = entry_data.get(CONF_DEVICE_ID)
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return Mazda6eOptionsFlow()


class Mazda6eOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    )
                }
            ),
        )
