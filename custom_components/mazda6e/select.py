"""Selects: seat heating / ventilation levels, pre-conditioning seat heating."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eControlEntity, Mazda6ePlanEntity, has_control, plan_entities
from .precondition import NO_WEATHER_ENTITY

LEVELS = ["off", "1", "2", "3"]


def _level(option: str) -> int:
    return 0 if option == "off" else int(option)


def _option(level: int | None) -> str | None:
    if level is None:
        return None
    return "off" if level == 0 else str(min(level, 3))


@dataclass(frozen=True, kw_only=True)
class Mazda6eSeatDescription(SelectEntityDescription):
    kind: str  # "heat" or "wind"
    seat: str  # "driver" or "passenger"
    field: str
    function: str  # function-config code


SEATS: tuple[Mazda6eSeatDescription, ...] = (
    Mazda6eSeatDescription(
        key="seat_heat_driver",
        translation_key="seat_heat_driver",
        icon="mdi:car-seat-heater",
        options=LEVELS,
        kind="heat",
        seat="driver",
        field="seat_heat_driver",
        function="#driverSeatHeat",
    ),
    Mazda6eSeatDescription(
        key="seat_heat_passenger",
        translation_key="seat_heat_passenger",
        icon="mdi:car-seat-heater",
        options=LEVELS,
        kind="heat",
        seat="passenger",
        field="seat_heat_passenger",
        function="#passengerSeatHeat",
    ),
    Mazda6eSeatDescription(
        key="seat_vent_driver",
        translation_key="seat_vent_driver",
        icon="mdi:car-seat-cooler",
        options=LEVELS,
        kind="wind",
        seat="driver",
        field="seat_vent_driver",
        function="#driverSeatVent",
    ),
    Mazda6eSeatDescription(
        key="seat_vent_passenger",
        translation_key="seat_vent_passenger",
        icon="mdi:car-seat-cooler",
        options=LEVELS,
        kind="wind",
        seat="passenger",
        field="seat_vent_passenger",
        function="#passengerSeatVent",
    ),
)

PLAN_SEAT_HEAT = SelectEntityDescription(
    key="precondition_seat_heat",
    translation_key="precondition_seat_heat",
    icon="mdi:car-seat-heater",
    options=LEVELS,
    entity_category=EntityCategory.CONFIG,
)

PLAN_WEATHER_ENTITY = SelectEntityDescription(
    key="precondition_weather_entity",
    translation_key="precondition_weather_entity",
    icon="mdi:weather-partly-cloudy",
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
    async_add_entities(
        Mazda6eSeatSelect(coordinator, vid, description)
        for vid, data in coordinator.data.items()
        for description in SEATS
        if data.supports(description.function)
    )
    async_add_entities(
        plan_entities(
            entry,
            lambda vid, p: (
                [Mazda6ePlanSeatHeat(coordinator, vid, PLAN_SEAT_HEAT, p)]
                if coordinator.data[vid].supports("#driverSeatHeat")
                else []
            ),
        )
    )
    async_add_entities(
        plan_entities(
            entry,
            lambda vid, p: [Mazda6ePlanWeatherEntity(coordinator, vid, PLAN_WEATHER_ENTITY, p)],
        )
    )


class Mazda6eSeatSelect(Mazda6eControlEntity, SelectEntity):
    entity_description: Mazda6eSeatDescription

    @property
    def current_option(self) -> str | None:
        return _option(self._value(self.entity_description.field))

    async def async_select_option(self, option: str) -> None:
        description = self.entity_description
        level = _level(option)
        await self._async_command(
            self.coordinator.client.set_seat(self._vehicle_id, description.kind, description.seat, level),
            {description.field: level},
        )


class Mazda6ePlanSeatHeat(Mazda6ePlanEntity, SelectEntity):
    @property
    def current_option(self) -> str | None:
        return _option(self.preconditioner.settings["seat_heat"])

    async def async_select_option(self, option: str) -> None:
        await self.preconditioner.async_update(seat_heat=_level(option))


class Mazda6ePlanWeatherEntity(Mazda6ePlanEntity, SelectEntity):
    """Weather entity that gates the weekly schedule by forecast temperature."""

    @property
    def options(self) -> list[str]:
        return [NO_WEATHER_ENTITY, *sorted(self.hass.states.async_entity_ids("weather"))]

    @property
    def current_option(self) -> str | None:
        return self.preconditioner.settings.get("weather_entity") or NO_WEATHER_ENTITY

    async def async_select_option(self, option: str) -> None:
        value = None if option == NO_WEATHER_ENTITY else option
        await self.preconditioner.async_update(weather_entity=value)
