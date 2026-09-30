"""Switches: defrost, steering wheel heating, the car's plans, pre-conditioning plan."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Mazda6eClient
from .coordinator import Mazda6eConfigEntry, VehicleData
from .entity import Mazda6eControlEntity, Mazda6ePlanEntity, has_control, plan_entities
from .precondition import WEEKDAYS, Preconditioner


@dataclass(frozen=True, kw_only=True)
class Mazda6eSwitchDescription(SwitchEntityDescription):
    field: str  # VehicleStatus field holding the state
    set_fn: Callable[[Mazda6eClient, str, bool], Awaitable[Any]]


SWITCHES: tuple[Mazda6eSwitchDescription, ...] = (
    Mazda6eSwitchDescription(
        key="defrost",
        translation_key="defrost",
        icon="mdi:car-defrost-front",
        field="defrost_on",
        set_fn=lambda client, vid, on: client.set_defrost(vid, on),
    ),
    Mazda6eSwitchDescription(
        key="steering_wheel_heat",
        translation_key="steering_wheel_heat",
        icon="mdi:steering",
        field="steering_wheel_heat_on",
        set_fn=lambda client, vid, on: client.set_steering_wheel_heat(vid, on),
    ),
)

BATTERY_PREHEAT = SwitchEntityDescription(
    key="battery_preheat", translation_key="battery_preheat", icon="mdi:battery-heart-variant"
)
CHARGE_SCHEDULE = SwitchEntityDescription(
    key="charge_schedule", translation_key="charge_schedule", icon="mdi:calendar-clock"
)

# setting -> description of the pre-conditioning plan switches
PLAN_SWITCHES: dict[str, SwitchEntityDescription] = {
    "enabled": SwitchEntityDescription(
        key="precondition_schedule", translation_key="precondition_schedule", icon="mdi:calendar-week"
    ),
    "steering_wheel": SwitchEntityDescription(
        key="precondition_steering_wheel",
        translation_key="precondition_steering_wheel",
        icon="mdi:steering",
        entity_category=EntityCategory.CONFIG,
    ),
    "defrost": SwitchEntityDescription(
        key="precondition_defrost",
        translation_key="precondition_defrost",
        icon="mdi:car-defrost-front",
        entity_category=EntityCategory.CONFIG,
    ),
    "battery": SwitchEntityDescription(
        key="precondition_battery",
        translation_key="precondition_battery",
        icon="mdi:battery-heart-variant",
        entity_category=EntityCategory.CONFIG,
    ),
}


def _day_switch(day: str) -> SwitchEntityDescription:
    return SwitchEntityDescription(
        key=f"precondition_{day}",
        translation_key=f"precondition_{day}",
        icon="mdi:calendar-today",
        entity_category=EntityCategory.CONFIG,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not has_control(entry):
        return
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for vid, data in coordinator.data.items():
        entities.extend(Mazda6eSwitch(coordinator, vid, d) for d in SWITCHES)
        if data.battery_preheat_plan is not None:
            entities.append(Mazda6eBatteryPreheatSwitch(coordinator, vid, BATTERY_PREHEAT))
        if data.status.charge_plan is not None:
            entities.append(Mazda6eChargeScheduleSwitch(coordinator, vid, CHARGE_SCHEDULE))
    async_add_entities(entities)

    def plan(vid: str, preconditioner: Preconditioner) -> list[SwitchEntity]:
        data: VehicleData = coordinator.data[vid]
        result: list[SwitchEntity] = [
            Mazda6ePlanSwitch(coordinator, vid, description, preconditioner, setting)
            for setting, description in PLAN_SWITCHES.items()
            if setting != "battery" or data.battery_preheat_plan is not None
        ]
        result.extend(Mazda6eDaySwitch(coordinator, vid, _day_switch(day), preconditioner, day) for day in WEEKDAYS)
        return result

    async_add_entities(plan_entities(entry, plan))


class Mazda6eSwitch(Mazda6eControlEntity, SwitchEntity):
    entity_description: Mazda6eSwitchDescription

    @property
    def is_on(self) -> bool | None:
        return self._value(self.entity_description.field)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        description = self.entity_description
        await self._async_command(
            description.set_fn(self.coordinator.client, self._vehicle_id, on),
            {description.field: on},
        )


class Mazda6eBatteryPreheatSwitch(Mazda6eControlEntity, SwitchEntity):
    """The car's own battery preheating plan (as in the app)."""

    @property
    def _plan(self) -> dict[str, Any] | None:
        return self.data.battery_preheat_plan if self.data else None

    @property
    def is_on(self) -> bool | None:
        plan = self._plan
        return None if plan is None else plan.get("isValid") == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        plan = self._plan
        await self._async_command(
            self.coordinator.client.set_battery_preheat(self._vehicle_id, plan, plan["endData"]), {}
        )
        plan["isValid"] = 1
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        plan = self._plan
        await self._async_command(self.coordinator.client.set_battery_preheat(self._vehicle_id, plan, None), {})
        plan["isValid"] = 0
        self.async_write_ha_state()


def charge_plan_enabled(plan: dict[str, Any]) -> bool:
    return plan.get("startSwitch") == 1 and plan.get("endSwitch") == 1


class Mazda6eChargeScheduleSwitch(Mazda6eControlEntity, SwitchEntity):
    """The car's charging schedule (start/end times are time entities)."""

    @property
    def is_on(self) -> bool | None:
        plan = self.status.charge_plan
        return None if plan is None else charge_plan_enabled(plan)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        plan = self.status.charge_plan
        await self._async_command(
            self.coordinator.client.set_charge_plan(
                self._vehicle_id,
                plan,
                start=str(plan.get("startTime") or "0000"),
                end=str(plan.get("endTime") or "0000"),
                enabled=on,
            ),
            {},
        )
        plan["startSwitch"] = plan["endSwitch"] = 1 if on else 0
        self.async_write_ha_state()


class Mazda6ePlanSwitch(Mazda6ePlanEntity, SwitchEntity):
    def __init__(self, coordinator, vehicle_id, description, preconditioner, setting: str) -> None:
        super().__init__(coordinator, vehicle_id, description, preconditioner)
        self._setting = setting

    @property
    def is_on(self) -> bool:
        return bool(self.preconditioner.settings[self._setting])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.preconditioner.async_update(**{self._setting: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.preconditioner.async_update(**{self._setting: False})


class Mazda6eDaySwitch(Mazda6ePlanEntity, SwitchEntity):
    def __init__(self, coordinator, vehicle_id, description, preconditioner, day: str) -> None:
        super().__init__(coordinator, vehicle_id, description, preconditioner)
        self._day = day

    @property
    def is_on(self) -> bool:
        return self.preconditioner.settings["days"][self._day]["on"]

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.preconditioner.async_update(day=self._day, on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.preconditioner.async_update(day=self._day, on=False)
