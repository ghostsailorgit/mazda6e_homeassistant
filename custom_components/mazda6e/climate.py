"""Remote cabin climate (pre-conditioning) for the Mazda 6e."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CLIMATE_MAX_TEMP, CLIMATE_MIN_TEMP
from .const import CONF_CONTROL_PRIVATE_KEY
from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eControlEntity

CABIN = EntityDescription(key="cabin_climate", translation_key="cabin_climate")

DEFAULT_TEMP = 21.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.data.get(CONF_CONTROL_PRIVATE_KEY):
        return
    coordinator = entry.runtime_data
    async_add_entities(Mazda6eClimate(coordinator, vehicle_id, CABIN) for vehicle_id in coordinator.data)


class Mazda6eClimate(Mazda6eControlEntity, ClimateEntity):
    """Starts or stops remote climate like the fan button in the app."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]  # noqa: RUF012 - HA attribute
    _attr_min_temp = CLIMATE_MIN_TEMP
    _attr_max_temp = CLIMATE_MAX_TEMP
    _attr_target_temperature_step = 0.5
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    # Last temperature requested here; the car reports none while climate is off.
    _last_target: float | None = None

    @property
    def hvac_mode(self) -> HVACMode | None:
        on = self._value("climate_on")
        if on is None:
            return None
        return HVACMode.HEAT_COOL if on else HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        return self.status.inside_temperature

    @property
    def target_temperature(self) -> float | None:
        return self._value("target_temperature") or self._last_target or DEFAULT_TEMP

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._async_set(hvac_mode != HVACMode.OFF, self.target_temperature)

    async def async_turn_on(self) -> None:
        await self._async_set(True, self.target_temperature)

    async def async_turn_off(self) -> None:
        await self._async_set(False, self.target_temperature)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = float(kwargs[ATTR_TEMPERATURE])
        mode = kwargs.get(ATTR_HVAC_MODE)
        if mode == HVACMode.OFF:
            self._last_target = temperature
            await self._async_set(False, temperature)
            return
        if mode is None and self.hvac_mode == HVACMode.OFF:
            # Only remember the temperature; don't start climate by surprise.
            self._last_target = temperature
            self.async_write_ha_state()
            return
        await self._async_set(True, temperature)

    async def _async_set(self, enabled: bool, temperature: float | None) -> None:
        temperature = temperature or DEFAULT_TEMP
        self._last_target = temperature
        optimistic: dict[str, Any] = {"climate_on": enabled}
        if enabled:
            optimistic["target_temperature"] = temperature
        await self._async_command(
            self.coordinator.client.set_climate(self._vehicle_id, enabled, temperature),
            optimistic,
        )
