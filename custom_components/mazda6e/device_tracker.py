"""Vehicle location for the Mazda 6e."""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eEntity

LOCATION = EntityDescription(key="location", translation_key="location", icon="mdi:car")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        Mazda6eTracker(coordinator, vehicle_id, LOCATION) for vehicle_id in coordinator.data
    )


class Mazda6eTracker(Mazda6eEntity, TrackerEntity):
    _attr_source_type = SourceType.GPS

    def _valid(self) -> bool:
        s = self.status
        # (0, 0) is what the backend reports when it has no fix.
        return s.latitude is not None and s.longitude is not None and (s.latitude, s.longitude) != (0, 0)

    @property
    def latitude(self) -> float | None:
        return self.status.latitude if self._valid() else None

    @property
    def longitude(self) -> float | None:
        return self.status.longitude if self._valid() else None
