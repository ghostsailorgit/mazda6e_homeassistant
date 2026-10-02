"""Config flow for the Mazda 6e integration."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .api import (
    CLIMATE_MAX_TEMP,
    CLIMATE_MIN_TEMP,
    Mazda6eClient,
    MazdaApiError,
    MazdaAuthError,
    MazdaConnectionError,
    MazdaError,
    MazdaPinError,
)
from .const import (
    CONF_CONTROL_PIN,
    CONF_CONTROL_PRIVATE_KEY,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_STORE_PIN,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    PLAN_ENABLED,
    PLAN_TEMPERATURE,
    PLAN_TIME,
    PLAN_VEHICLE,
    PLAN_WEEKDAYS,
    REGION_ASIA,
    REGION_EUROPE,
    SUBENTRY_PLAN,
)
from .crypto import generate_key_pair
from .precondition import WEEKDAYS

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

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        # Pre-conditioning sends remote commands, which need the control key.
        if not config_entry.data.get(CONF_CONTROL_PRIVATE_KEY):
            return {}
        return {SUBENTRY_PLAN: DeparturePlanFlow}


def _plan_schema(defaults: Mapping[str, Any], vehicles: dict[str, str]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): TextSelector(),
        vol.Required(PLAN_TIME, default=defaults.get(PLAN_TIME, "07:30:00")): TimeSelector(),
        vol.Required(PLAN_WEEKDAYS, default=list(defaults.get(PLAN_WEEKDAYS, WEEKDAYS[:5]))): SelectSelector(
            SelectSelectorConfig(
                options=list(WEEKDAYS),
                multiple=True,
                mode=SelectSelectorMode.LIST,
                translation_key="weekday",
            )
        ),
        vol.Required(PLAN_TEMPERATURE, default=defaults.get(PLAN_TEMPERATURE, 21.0)): NumberSelector(
            NumberSelectorConfig(
                min=CLIMATE_MIN_TEMP,
                max=CLIMATE_MAX_TEMP,
                step=0.5,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="°C",
            )
        ),
    }
    if len(vehicles) > 1:
        fields[vol.Required(PLAN_VEHICLE, default=defaults.get(PLAN_VEHICLE, next(iter(vehicles))))] = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=vid, label=name) for vid, name in vehicles.items()],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    return vol.Schema(fields)


def _plan_errors(user_input: Mapping[str, Any]) -> dict[str, str]:
    if not user_input[CONF_NAME].strip():
        return {CONF_NAME: "name_required"}
    if not user_input[PLAN_WEEKDAYS]:
        return {PLAN_WEEKDAYS: "weekday_required"}
    return {}


def _plan_data(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        PLAN_TIME: user_input[PLAN_TIME][:5],
        PLAN_WEEKDAYS: [day for day in WEEKDAYS if day in user_input[PLAN_WEEKDAYS]],
        PLAN_TEMPERATURE: float(user_input[PLAN_TEMPERATURE]),
    }


class DeparturePlanFlow(ConfigSubentryFlow):
    """Add or change a departure plan: name, time, weekdays and temperature."""

    def _vehicles(self) -> dict[str, str]:
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return {}
        coordinator = entry.runtime_data
        return {vid: coordinator.data[vid].vehicle.display_name for vid in coordinator.preconditioners}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        vehicles = self._vehicles()
        if not vehicles:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _plan_errors(user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip(),
                    data={
                        **_plan_data(user_input),
                        PLAN_VEHICLE: user_input.get(PLAN_VEHICLE, next(iter(vehicles))),
                        PLAN_ENABLED: True,
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=_plan_schema(user_input or {}, vehicles), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _plan_errors(user_input)
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=user_input[CONF_NAME].strip(),
                    data={**subentry.data, **_plan_data(user_input)},
                )
        defaults = user_input or {
            **subentry.data,
            CONF_NAME: subentry.title,
            PLAN_TIME: f"{subentry.data[PLAN_TIME]}:00",
        }
        return self.async_show_form(step_id="reconfigure", data_schema=_plan_schema(defaults, {}), errors=errors)


class Mazda6eOptionsFlow(OptionsFlow):
    """Poll interval and the optional stored control PIN."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self.config_entry.options
        errors: dict[str, str] = {}
        placeholders = {"attempts": "?"}

        if user_input is not None:
            new_options = {CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            pin = (user_input.get(CONF_CONTROL_PIN) or "").strip()
            if user_input.get(CONF_STORE_PIN):
                if pin:
                    errors = await self._async_check_pin(pin, placeholders)
                    new_options[CONF_CONTROL_PIN] = pin
                elif options.get(CONF_CONTROL_PIN):
                    new_options[CONF_CONTROL_PIN] = options[CONF_CONTROL_PIN]
                else:
                    errors[CONF_CONTROL_PIN] = "pin_required"
            if not errors:
                return self.async_create_entry(data=new_options)

        return self.async_show_form(
            step_id="init",
            errors=errors,
            description_placeholders=placeholders,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                    vol.Required(
                        CONF_STORE_PIN, default=bool(options.get(CONF_CONTROL_PIN))
                    ): BooleanSelector(),
                    vol.Optional(CONF_CONTROL_PIN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )

    async def _async_check_pin(self, pin: str, placeholders: dict[str, str]) -> dict[str, str]:
        """Validate the PIN against the backend so typos show up right away."""
        if len(pin) != 6 or not pin.isdigit():
            return {CONF_CONTROL_PIN: "invalid_pin_format"}
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None:
            return {}  # integration not loaded, can't check now
        try:
            await coordinator.client.get_rc_token(pin)
        except MazdaPinError as err:
            if err.attempts_left is not None:
                placeholders["attempts"] = str(err.attempts_left)
                return {CONF_CONTROL_PIN: "invalid_pin_attempts"}
            return {CONF_CONTROL_PIN: "invalid_pin"}
        except MazdaConnectionError:
            return {"base": "cannot_connect"}
        except MazdaError:
            _LOGGER.exception("Could not verify the control PIN")
            return {"base": "unknown"}
        return {}
