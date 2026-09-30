"""Sensors for the Mazda 6e integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eEntity
from .models import CHARGE_STATUS, POSITIONS, VehicleStatus


@dataclass(frozen=True, kw_only=True)
class Mazda6eSensorDescription(SensorEntityDescription):
    value_fn: Callable[[VehicleStatus], Any]


def _tire(position: str) -> Mazda6eSensorDescription:
    return Mazda6eSensorDescription(
        key=f"tire_pressure_{position}",
        translation_key=f"tire_pressure_{position}",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.BAR,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s, p=position: s.tire_pressure_bar.get(p),
    )


SENSORS: tuple[Mazda6eSensorDescription, ...] = (
    Mazda6eSensorDescription(
        key="soc",
        translation_key="soc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.soc,
    ),
    Mazda6eSensorDescription(
        key="range",
        translation_key="range",
        icon="mdi:map-marker-distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        value_fn=lambda s: s.range_km,
    ),
    Mazda6eSensorDescription(
        key="odometer",
        translation_key="odometer",
        icon="mdi:counter",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        value_fn=lambda s: s.odometer_km,
    ),
    Mazda6eSensorDescription(
        key="charge_current",
        translation_key="charge_current",
        icon="mdi:current-ac",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=lambda s: s.charge_current,
    ),
    Mazda6eSensorDescription(
        key="ac_charge_current",
        translation_key="ac_charge_current",
        icon="mdi:current-ac",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.ac_charge_current,
    ),
    Mazda6eSensorDescription(
        key="dc_charge_current",
        translation_key="dc_charge_current",
        icon="mdi:current-dc",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.dc_charge_current,
    ),
    Mazda6eSensorDescription(
        key="charge_status",
        translation_key="charge_status",
        icon="mdi:ev-station",
        device_class=SensorDeviceClass.ENUM,
        options=[*CHARGE_STATUS.values(), "unknown"],
        value_fn=lambda s: s.charge_status,
    ),
    Mazda6eSensorDescription(
        key="remaining_charge_time",
        translation_key="remaining_charge_time",
        icon="mdi:timer-sand",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda s: s.remaining_charge_minutes,
    ),
    Mazda6eSensorDescription(
        key="charge_limit",
        translation_key="charge_limit",
        icon="mdi:battery-charging-80",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.charge_limit,
    ),
    Mazda6eSensorDescription(
        key="inside_temperature",
        translation_key="inside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: s.inside_temperature,
    ),
    Mazda6eSensorDescription(
        key="speed",
        translation_key="speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.speed_kmh,
    ),
    Mazda6eSensorDescription(
        key="vehicle_state",
        translation_key="vehicle_state",
        icon="mdi:car-info",
        device_class=SensorDeviceClass.ENUM,
        options=["driving", "parked"],
        value_fn=lambda s: s.vehicle_state,
    ),
    *(_tire(p) for p in POSITIONS),
    Mazda6eSensorDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_update,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        Mazda6eSensor(coordinator, vehicle_id, description)
        for vehicle_id in coordinator.data
        for description in SENSORS
    )


class Mazda6eSensor(Mazda6eEntity, SensorEntity):
    entity_description: Mazda6eSensorDescription

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.status)
