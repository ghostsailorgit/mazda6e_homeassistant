"""Services, so automations can drive pre-conditioning from any trigger."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from .api import CLIMATE_MAX_TEMP, CLIMATE_MIN_TEMP, MazdaError
from .const import DOMAIN
from .precondition import LEAD_MAX, LEAD_MIN, Preconditioner

SERVICE_START = "start_preconditioning"
SERVICE_STOP = "stop_preconditioning"
SERVICE_SKIP = "skip_next_departure"
SERVICE_RAW = "send_raw_command"

ATTR_DURATION = "duration"
ATTR_SEAT_HEAT = "seat_heat"
ATTR_STEERING_WHEEL = "steering_wheel"
ATTR_DEFROST = "defrost"
ATTR_BATTERY = "battery"

TARGET = {vol.Optional(ATTR_DEVICE_ID): cv.string}

START_SCHEMA = vol.Schema(
    {
        **TARGET,
        vol.Optional(ATTR_TEMPERATURE): vol.All(
            vol.Coerce(float), vol.Range(min=CLIMATE_MIN_TEMP, max=CLIMATE_MAX_TEMP)
        ),
        vol.Optional(ATTR_DURATION): vol.All(vol.Coerce(int), vol.Range(min=LEAD_MIN, max=LEAD_MAX)),
        vol.Optional(ATTR_SEAT_HEAT): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
        vol.Optional(ATTR_STEERING_WHEEL): cv.boolean,
        vol.Optional(ATTR_DEFROST): cv.boolean,
        vol.Optional(ATTR_BATTERY): cv.boolean,
    }
)
TARGET_SCHEMA = vol.Schema(TARGET)
RAW_SCHEMA = vol.Schema(
    {
        **TARGET,
        vol.Required("control"): cv.string,
        vol.Optional("params", default={}): vol.Schema({cv.string: object}),
        vol.Optional("needs_pin", default=True): cv.boolean,
    }
)


def _preconditioners(hass: HomeAssistant) -> dict[str, Preconditioner]:
    """All loaded pre-conditioners, keyed by the car's VIN."""
    result = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = entry.runtime_data
        for vehicle_id, preconditioner in coordinator.preconditioners.items():
            result[coordinator.data[vehicle_id].vehicle.vin] = preconditioner
    return result


def _resolve(hass: HomeAssistant, call: ServiceCall) -> Preconditioner:
    cars = _preconditioners(hass)
    device_id = call.data.get(ATTR_DEVICE_ID)
    if device_id is None:
        if len(cars) == 1:
            return next(iter(cars.values()))
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_required" if cars else "no_control",
        )
    device = dr.async_get(hass).async_get(device_id)
    vin = next((ident for domain, ident in device.identifiers if domain == DOMAIN), None) if device else None
    if vin not in cars:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")
    return cars[vin]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def start(call: ServiceCall) -> dict[str, Any]:
        preconditioner = _resolve(hass, call)
        failed = await preconditioner.async_start(
            source="service",
            temperature=call.data.get(ATTR_TEMPERATURE),
            duration=call.data.get(ATTR_DURATION),
            seat_heat=call.data.get(ATTR_SEAT_HEAT),
            steering_wheel=call.data.get(ATTR_STEERING_WHEEL),
            defrost=call.data.get(ATTR_DEFROST),
            battery=call.data.get(ATTR_BATTERY),
        )
        return {"failed": failed}

    async def stop(call: ServiceCall) -> dict[str, Any]:
        failed = await _resolve(hass, call).async_stop(source="service")
        return {"failed": failed}

    async def skip(call: ServiceCall) -> None:
        await _resolve(hass, call).async_skip_next()

    hass.services.async_register(
        DOMAIN, SERVICE_START, start, schema=START_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP, stop, schema=TARGET_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_SKIP, skip, schema=TARGET_SCHEMA)

    async def raw(call: ServiceCall) -> dict[str, Any]:
        """Diagnostics: send any remote command and return the car's answer."""
        preconditioner = _resolve(hass, call)
        coordinator = preconditioner.coordinator
        try:
            result = await coordinator.client.send_raw_command(
                preconditioner.vehicle_id,
                call.data["control"],
                dict(call.data["params"]),
                needs_pin=call.data["needs_pin"],
            )
        except (MazdaError, ValueError) as err:
            return {"success": False, "error": str(err), "code": getattr(err, "code", None)}
        await coordinator.async_request_refresh()
        return {"success": True, "result": result}

    async_register_admin_service(
        hass, DOMAIN, SERVICE_RAW, raw, schema=RAW_SCHEMA, supports_response=SupportsResponse.ONLY
    )
