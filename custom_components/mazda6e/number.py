"""Numbers: charge limit and pre-conditioning settings."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CHARGE_LIMIT_MAX, CHARGE_LIMIT_MIN, CLIMATE_MAX_TEMP, CLIMATE_MIN_TEMP
from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eControlEntity, Mazda6ePlanEntity, has_control, plan_entities
from .precondition import LEAD_MAX, LEAD_MIN

CHARGE_LIMIT = NumberEntityDescription(
    key="charge_limit_control",
    translation_key="charge_limit_control",
    icon="mdi:battery-charging-80",
    native_min_value=CHARGE_LIMIT_MIN,
    native_max_value=CHARGE_LIMIT_MAX,
    native_step=1,
    native_unit_of_measurement=PERCENTAGE,
    mode=NumberMode.SLIDER,
)

# key -> setting of the pre-conditioner
PLAN_NUMBERS: dict[str, NumberEntityDescription] = {
    "temperature": NumberEntityDescription(
        key="precondition_temperature",
        translation_key="precondition_temperature",
        icon="mdi:thermometer",
        native_min_value=CLIMATE_MIN_TEMP,
        native_max_value=CLIMATE_MAX_TEMP,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    "lead": NumberEntityDescription(
        key="precondition_lead",
        translation_key="precondition_lead",
        icon="mdi:timer-outline",
        native_min_value=LEAD_MIN,
        native_max_value=LEAD_MAX,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    "cold_below": NumberEntityDescription(
        key="precondition_cold_below",
        translation_key="precondition_cold_below",
        icon="mdi:thermometer-low",
        native_min_value=-20,
        native_max_value=30,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not has_control(entry):
        return
    coordinator = entry.runtime_data
    async_add_entities(Mazda6eChargeLimit(coordinator, vid, CHARGE_LIMIT) for vid in coordinator.data)
    async_add_entities(
        plan_entities(
            entry,
            lambda vid, p: [
                Mazda6ePlanNumber(coordinator, vid, description, p, setting)
                for setting, description in PLAN_NUMBERS.items()
            ],
        )
    )


class Mazda6eChargeLimit(Mazda6eControlEntity, NumberEntity):
    @property
    def native_value(self) -> float | None:
        return self._value("charge_limit")

    async def async_set_native_value(self, value: float) -> None:
        limit = int(value)
        await self._async_command(
            self.coordinator.client.set_charge_limit(self._vehicle_id, limit),
            {"charge_limit": limit},
        )


class Mazda6ePlanNumber(Mazda6ePlanEntity, NumberEntity):
    def __init__(self, coordinator, vehicle_id, description, preconditioner, setting: str) -> None:
        super().__init__(coordinator, vehicle_id, description, preconditioner)
        self._setting = setting

    @property
    def native_value(self) -> float:
        return self.preconditioner.settings[self._setting]

    async def async_set_native_value(self, value: float) -> None:
        if self._setting == "lead":
            value = int(value)
        await self.preconditioner.async_update(**{self._setting: value})
