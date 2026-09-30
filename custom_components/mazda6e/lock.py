"""Remote door lock for the Mazda 6e."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import ATTR_CODE, LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_CONTROL_PRIVATE_KEY
from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eControlEntity

DOORS = EntityDescription(key="door_lock", translation_key="door_lock")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.data.get(CONF_CONTROL_PRIVATE_KEY):
        return
    coordinator = entry.runtime_data
    async_add_entities(Mazda6eLock(coordinator, vehicle_id, DOORS) for vehicle_id in coordinator.data)


class Mazda6eLock(Mazda6eControlEntity, LockEntity):
    @property
    def code_format(self) -> str | None:
        # Without a stored PIN, Home Assistant asks for it on every action.
        return None if self.coordinator.client.control_pin else r"^\d{6}$"

    @property
    def is_locked(self) -> bool | None:
        return self._value("locked")

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_set_locked(True, kwargs.get(ATTR_CODE))

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._async_set_locked(False, kwargs.get(ATTR_CODE))

    async def _async_set_locked(self, locked: bool, code: str | None) -> None:
        if locked:
            self._attr_is_locking = True
        else:
            self._attr_is_unlocking = True
        self.async_write_ha_state()
        try:
            await self._async_command(
                self.coordinator.client.set_locked(self._vehicle_id, locked, pin=code or None),
                {"locked": locked},
                pin=code,
            )
        finally:
            self._attr_is_locking = False
            self._attr_is_unlocking = False
            self.async_write_ha_state()
