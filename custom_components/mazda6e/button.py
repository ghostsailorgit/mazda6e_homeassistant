"""Buttons: fresh status, find the car, pre-conditioning actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .api import FLASH_AND_HONK, FLASH_ONLY, Mazda6eClient
from .const import DOMAIN
from .coordinator import Mazda6eConfigEntry, VehicleData
from .entity import Mazda6eControlEntity, Mazda6ePlanEntity, has_control, plan_entities
from .precondition import Preconditioner

# The car needs a moment to upload its status after being asked.
STATUS_UPLOAD_DELAY = timedelta(seconds=30)


@dataclass(frozen=True, kw_only=True)
class Mazda6eButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[Mazda6eClient, str], Awaitable[Any]]
    supported_fn: Callable[[VehicleData], bool] = lambda _: True


BUTTONS: tuple[Mazda6eButtonDescription, ...] = (
    Mazda6eButtonDescription(
        key="refresh_status",
        translation_key="refresh_status",
        icon="mdi:car-connected",
        press_fn=lambda client, vid: client.request_status_update(vid),
    ),
    Mazda6eButtonDescription(
        key="flash",
        translation_key="flash",
        icon="mdi:car-light-high",
        press_fn=lambda client, vid: client.flash_and_honk(vid, FLASH_ONLY),
        supported_fn=lambda data: data.supports("#findCar"),
    ),
    Mazda6eButtonDescription(
        key="flash_honk",
        translation_key="flash_honk",
        icon="mdi:bullhorn",
        press_fn=lambda client, vid: client.flash_and_honk(vid, FLASH_AND_HONK),
        supported_fn=lambda data: data.supports("#findCar"),
    ),
)


@dataclass(frozen=True, kw_only=True)
class Mazda6ePlanButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[Preconditioner], Awaitable[Any]]


PLAN_BUTTONS: tuple[Mazda6ePlanButtonDescription, ...] = (
    Mazda6ePlanButtonDescription(
        key="precondition_start",
        translation_key="precondition_start",
        icon="mdi:car-seat-heater",
        press_fn=lambda p: p.async_start(source="button"),
    ),
    Mazda6ePlanButtonDescription(
        key="precondition_stop",
        translation_key="precondition_stop",
        icon="mdi:stop-circle-outline",
        press_fn=lambda p: p.async_stop(source="button"),
    ),
    Mazda6ePlanButtonDescription(
        key="precondition_skip",
        translation_key="precondition_skip",
        icon="mdi:skip-next",
        press_fn=lambda p: p.async_skip_next(),
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
        Mazda6eButton(coordinator, vid, description)
        for vid, data in coordinator.data.items()
        for description in BUTTONS
        if description.supported_fn(data)
    )
    async_add_entities(
        plan_entities(
            entry,
            lambda vid, p: [Mazda6ePlanButton(coordinator, vid, d, p) for d in PLAN_BUTTONS],
        )
    )


class Mazda6eButton(Mazda6eControlEntity, ButtonEntity):
    entity_description: Mazda6eButtonDescription

    async def async_press(self) -> None:
        await self._async_command(
            self.entity_description.press_fn(self.coordinator.client, self._vehicle_id), {}
        )
        if self.entity_description.key == "refresh_status":

            async def refresh(_now: Any) -> None:
                await self.coordinator.async_request_refresh()

            async_call_later(self.hass, STATUS_UPLOAD_DELAY, refresh)


class Mazda6ePlanButton(Mazda6ePlanEntity, ButtonEntity):
    entity_description: Mazda6ePlanButtonDescription

    async def async_press(self) -> None:
        failed = await self.entity_description.press_fn(self.preconditioner)
        if failed:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="precondition_failed",
                translation_placeholders={"steps": ", ".join(failed)},
            )
