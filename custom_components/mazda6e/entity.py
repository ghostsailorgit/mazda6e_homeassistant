"""Base entity for the Mazda 6e integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Mazda6eCoordinator, VehicleData
from .models import VehicleStatus


class Mazda6eEntity(CoordinatorEntity[Mazda6eCoordinator]):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Mazda6eCoordinator,
        vehicle_id: str,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._vehicle_id = vehicle_id
        vehicle = coordinator.data[vehicle_id].vehicle
        self._attr_unique_id = f"{vehicle.vin}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vehicle.vin)},
            manufacturer="Mazda",
            model=vehicle.model_name or vehicle.series_name or "6e",
            name=vehicle.display_name,
            serial_number=vehicle.vin,
        )

    @property
    def _data(self) -> VehicleData | None:
        return (self.coordinator.data or {}).get(self._vehicle_id)

    @property
    def status(self) -> VehicleStatus:
        data = self._data
        return data.status if data else VehicleStatus()

    @property
    def available(self) -> bool:
        return super().available and self._data is not None
