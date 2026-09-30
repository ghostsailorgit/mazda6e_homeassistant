"""Covers: windows and tailgate. Both need the stored control PIN."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityDescription,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Mazda6eClient
from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eControlEntity, has_control


@dataclass(frozen=True, kw_only=True)
class Mazda6eCoverDescription(CoverEntityDescription):
    field: str  # VehicleStatus field that is True while open
    set_fn: Callable[[Mazda6eClient, str, bool], Awaitable[Any]]


COVERS: tuple[Mazda6eCoverDescription, ...] = (
    Mazda6eCoverDescription(
        key="windows_control",
        translation_key="windows_control",
        device_class=CoverDeviceClass.WINDOW,
        field="any_window_open",
        set_fn=lambda client, vid, open_: client.set_windows(vid, open_),
    ),
    Mazda6eCoverDescription(
        key="trunk_control",
        translation_key="trunk_control",
        device_class=CoverDeviceClass.DOOR,
        icon="mdi:car-back",
        field="trunk_open",
        set_fn=lambda client, vid, open_: client.set_trunk(vid, open_),
    ),
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
        Mazda6eCover(coordinator, vid, description) for vid in coordinator.data for description in COVERS
    )


class Mazda6eCover(Mazda6eControlEntity, CoverEntity):
    """Covers can't ask for a code, so the PIN has to be stored in the options."""

    entity_description: Mazda6eCoverDescription
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    @property
    def is_closed(self) -> bool | None:
        open_ = self._value(self.entity_description.field)
        return None if open_ is None else not open_

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, open_: bool) -> None:
        if open_:
            self._attr_is_opening = True
        else:
            self._attr_is_closing = True
        self.async_write_ha_state()
        try:
            await self._async_command(
                self.entity_description.set_fn(self.coordinator.client, self._vehicle_id, open_),
                {self.entity_description.field: open_},
            )
        finally:
            self._attr_is_opening = self._attr_is_closing = False
            self.async_write_ha_state()
