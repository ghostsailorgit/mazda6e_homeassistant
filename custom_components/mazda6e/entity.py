"""Base entity for the Mazda 6e integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import MazdaAuthError, MazdaError, MazdaPinError
from .const import CONF_CONTROL_PRIVATE_KEY, DOMAIN, PLAN_VEHICLE, SUBENTRY_PLAN
from .coordinator import Mazda6eConfigEntry, Mazda6eCoordinator, VehicleData
from .models import Vehicle, VehicleStatus
from .precondition import Preconditioner


def car_device_info(vehicle: Vehicle) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, vehicle.vin)},
        manufacturer="Mazda",
        model=vehicle.model_name or vehicle.series_name or "6e",
        name=vehicle.display_name,
        serial_number=vehicle.vin,
    )


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
        self._attr_device_info = car_device_info(vehicle)

    @property
    def _data(self) -> VehicleData | None:
        return (self.coordinator.data or {}).get(self._vehicle_id)

    @property
    def data(self) -> VehicleData | None:
        return self._data

    @property
    def status(self) -> VehicleStatus:
        data = self._data
        return data.status if data else VehicleStatus()

    @property
    def available(self) -> bool:
        return super().available and self._data is not None


def has_control(entry: Mazda6eConfigEntry) -> bool:
    """Remote commands need the key pair registered at login."""
    return bool(entry.data.get(CONF_CONTROL_PRIVATE_KEY))


# The car often reports a changed state only minutes after confirming a
# command; show the commanded state until a newer report arrives.
OPTIMISTIC_HOLD = timedelta(minutes=10)


class Mazda6eControlEntity(Mazda6eEntity):
    """Entity that sends remote commands and shows their result optimistically."""

    _optimistic: dict[str, Any] | None = None
    _commanded_at: datetime | None = None

    def _value(self, field: str) -> Any:
        """VehicleStatus field, overridden by a pending commanded value."""
        if self._optimistic and field in self._optimistic:
            return self._optimistic[field]
        return getattr(self.status, field)

    async def _async_command(
        self, command: Awaitable[Any], optimistic: dict[str, Any], *, pin: str | None = None
    ) -> None:
        """Run a command, map errors to translated messages, refresh afterwards.

        ``optimistic`` maps VehicleStatus fields to the values the command sets.
        """
        try:
            await command
        except MazdaPinError as err:
            if err.attempts_left is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_pin_attempts",
                    translation_placeholders={"attempts": str(err.attempts_left)},
                ) from err
            key = "pin_missing" if not (pin or self.coordinator.client.control_pin) else "invalid_pin"
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=key) from err
        except MazdaAuthError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="reauth_required") from err
        except MazdaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        else:
            self._optimistic = optimistic
            self._commanded_at = dt_util.utcnow()
        finally:
            self.async_write_ha_state()

        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None and self._commanded_at is not None:
            status = self.status
            reported = status.last_update
            if (
                all(getattr(status, k) == v for k, v in self._optimistic.items())
                or (reported is not None and reported > self._commanded_at)
                or dt_util.utcnow() - self._commanded_at > OPTIMISTIC_HOLD
            ):
                self._optimistic = None
                self._commanded_at = None
        super()._handle_coordinator_update()


class Mazda6ePlanEntity(Mazda6eEntity):
    """Setting or action of the pre-conditioning plan (stored in Home Assistant)."""

    def __init__(
        self,
        coordinator: Mazda6eCoordinator,
        vehicle_id: str,
        description: EntityDescription,
        preconditioner: Preconditioner,
    ) -> None:
        super().__init__(coordinator, vehicle_id, description)
        self.preconditioner = preconditioner

    @property
    def available(self) -> bool:
        # The plan lives in Home Assistant and works while the cloud is down.
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.preconditioner.async_add_listener(self.async_write_ha_state))


def plan_entities(
    entry: Mazda6eConfigEntry, factory: Callable[[str, Preconditioner], list[Any]]
) -> list[Any]:
    """Build plan entities for every car that has a pre-conditioner."""
    coordinator = entry.runtime_data
    return [
        entity
        for vehicle_id, preconditioner in coordinator.preconditioners.items()
        for entity in factory(vehicle_id, preconditioner)
    ]


class Mazda6eDeparturePlanEntity(Mazda6ePlanEntity):
    """On/off, time or temperature of one departure plan (a config subentry)."""

    def __init__(
        self,
        coordinator: Mazda6eCoordinator,
        vehicle_id: str,
        description: EntityDescription,
        preconditioner: Preconditioner,
        subentry_id: str,
    ) -> None:
        super().__init__(coordinator, vehicle_id, description, preconditioner)
        self._subentry_id = subentry_id
        # A device belongs to one subentry, so each plan is its own device
        # hanging off the car instead of sharing the car's device.
        vehicle = coordinator.data[vehicle_id].vehicle
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{vehicle.vin}_{subentry_id}")},
            name=f"{vehicle.display_name} {coordinator.config_entry.subentries[subentry_id].title}",
            entry_type=DeviceEntryType.SERVICE,
        )
        if "via_device_id" in DeviceInfo.__annotations__:  # HA 2026.10+
            self._attr_device_info["via_device_id"] = coordinator.car_device_ids[vehicle_id]
        else:
            self._attr_device_info["via_device"] = (DOMAIN, vehicle.vin)

    @property
    def plan_data(self) -> Mapping[str, Any]:
        return self.coordinator.config_entry.subentries[self._subentry_id].data

    @callback
    def update_plan(self, **changes: Any) -> None:
        """Store a change; the entry's update listener plans again."""
        entry = self.coordinator.config_entry
        subentry = entry.subentries[self._subentry_id]
        self.hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, **changes})


def add_departure_plan_entities(
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str, Preconditioner, str], list[Any]],
) -> None:
    """Add the entities of every departure plan, attached to its subentry."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_PLAN:
            continue
        preconditioner = coordinator.preconditioners.get(subentry.data.get(PLAN_VEHICLE))
        if preconditioner is None:
            continue
        async_add_entities(
            factory(preconditioner.vehicle_id, preconditioner, subentry_id),
            config_subentry_id=subentry_id,
        )
