"""Diagnostics: dump the raw backend data to help map unknown fields."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONTROL_PRIVATE_KEY,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
)
from .coordinator import Mazda6eConfigEntry

TO_REDACT = {
    CONF_EMAIL,
    CONF_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_DEVICE_ID,
    CONF_CONTROL_PRIVATE_KEY,
    "vin",
    "plate_number",
    "latitude",
    "longitude",
    "lat",
    "lng",
    "lon",
    "gpsLatitude",
    "gpsLongitude",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Mazda6eConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    vehicles = {
        f"vehicle_{i}": {
            "model_name": data.vehicle.model_name,
            "series_name": data.vehicle.series_name,
            "raw_status": data.status.raw,
        }
        for i, data in enumerate((coordinator.data or {}).values())
    }
    return async_redact_data(
        {"entry": dict(entry.data), "options": dict(entry.options), "vehicles": vehicles},
        TO_REDACT,
    )
