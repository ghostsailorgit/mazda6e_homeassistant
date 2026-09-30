"""Binary sensors (doors, windows, locks, charging) for the Mazda 6e."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eEntity
from .models import POSITIONS, VehicleStatus


def _not(value: bool | None) -> bool | None:
    return None if value is None else not value


@dataclass(frozen=True, kw_only=True)
class Mazda6eBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[VehicleStatus], bool | None]


def _door(position: str) -> Mazda6eBinarySensorDescription:
    return Mazda6eBinarySensorDescription(
        key=f"door_{position}",
        translation_key=f"door_{position}",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda s, p=position: s.doors_open.get(p),
    )


def _window(position: str) -> Mazda6eBinarySensorDescription:
    return Mazda6eBinarySensorDescription(
        key=f"window_{position}",
        translation_key=f"window_{position}",
        device_class=BinarySensorDeviceClass.WINDOW,
        value_fn=lambda s, p=position: s.windows_open.get(p),
    )


BINARY_SENSORS: tuple[Mazda6eBinarySensorDescription, ...] = (
    # LOCK device class: on = unlocked
    Mazda6eBinarySensorDescription(
        key="lock",
        translation_key="lock",
        device_class=BinarySensorDeviceClass.LOCK,
        value_fn=lambda s: _not(s.locked),
    ),
    Mazda6eBinarySensorDescription(
        key="doors",
        translation_key="doors",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda s: s.any_door_open,
    ),
    Mazda6eBinarySensorDescription(
        key="windows",
        translation_key="windows",
        device_class=BinarySensorDeviceClass.WINDOW,
        value_fn=lambda s: s.any_window_open,
    ),
    *(_door(p) for p in POSITIONS),
    Mazda6eBinarySensorDescription(
        key="trunk",
        translation_key="trunk",
        device_class=BinarySensorDeviceClass.OPENING,
        value_fn=lambda s: s.trunk_open,
    ),
    Mazda6eBinarySensorDescription(
        key="hood",
        translation_key="hood",
        device_class=BinarySensorDeviceClass.OPENING,
        value_fn=lambda s: s.hood_open,
    ),
    *(_window(p) for p in POSITIONS),
    Mazda6eBinarySensorDescription(
        key="plugged_in",
        translation_key="plugged_in",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda s: s.plugged_in,
    ),
    Mazda6eBinarySensorDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda s: s.is_charging,
    ),
    Mazda6eBinarySensorDescription(
        key="climate",
        translation_key="climate",
        icon="mdi:air-conditioner",
        value_fn=lambda s: s.climate_on,
    ),
    Mazda6eBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.online,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        Mazda6eBinarySensor(coordinator, vehicle_id, description)
        for vehicle_id in coordinator.data
        for description in BINARY_SENSORS
    )


class Mazda6eBinarySensor(Mazda6eEntity, BinarySensorEntity):
    entity_description: Mazda6eBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.status)
