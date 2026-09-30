"""Polling coordinator for the Mazda 6e integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Mazda6eClient, MazdaAuthError, MazdaError
from .const import DOMAIN
from .models import Vehicle, VehicleStatus

_LOGGER = logging.getLogger(__name__)


@dataclass
class VehicleData:
    vehicle: Vehicle
    status: VehicleStatus


type Mazda6eConfigEntry = ConfigEntry[Mazda6eCoordinator]


class Mazda6eCoordinator(DataUpdateCoordinator[dict[str, VehicleData]]):
    """Fetches the vehicle list once and the status of every car on each poll."""

    config_entry: Mazda6eConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Mazda6eConfigEntry,
        client: Mazda6eClient,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=interval,
        )
        self.client = client
        self._vehicles: list[Vehicle] | None = None

    async def _async_update_data(self) -> dict[str, VehicleData]:
        try:
            if self._vehicles is None:
                self._vehicles = await self.client.get_vehicles()
                if not self._vehicles:
                    raise UpdateFailed("No vehicle found on this Mazda account")

            result: dict[str, VehicleData] = {}
            for vehicle in self._vehicles:
                status = await self.client.get_status(vehicle.vehicle_id)
                result[vehicle.vehicle_id] = VehicleData(vehicle, status)
            return result
        except MazdaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MazdaError as err:
            raise UpdateFailed(str(err)) from err
