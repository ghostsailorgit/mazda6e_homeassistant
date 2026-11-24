"""Polling coordinator for the Mazda 6e integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Mazda6eClient, MazdaApiError, MazdaAuthError, MazdaError, MazdaRefreshError
from .const import DOMAIN
from .models import Vehicle, VehicleStatus

if TYPE_CHECKING:
    from .precondition import Preconditioner

_LOGGER = logging.getLogger(__name__)

# A refused token refresh is retried this long before a new login is requested.
# The backend sometimes refuses refreshes for a few minutes (seen at night);
# asking for a login right away left the integration dead until the next morning.
REFRESH_GRACE = timedelta(hours=1)


@dataclass
class VehicleData:
    vehicle: Vehicle
    status: VehicleStatus
    # function codes from function-config; empty = unknown, assume everything
    functions: set[str] = field(default_factory=set)
    battery_preheat_plan: dict[str, Any] | None = None

    def supports(self, *codes: str) -> bool:
        return not self.functions or any(code in self.functions for code in codes)


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
        self._functions: dict[str, set[str]] = {}
        # vehicle_id -> Preconditioner, filled in async_setup_entry
        self.preconditioners: dict[str, Preconditioner] = {}
        # departure plan subentry id -> title, as the entities were set up
        self.plan_titles: dict[str, str] = {}
        # vehicle_id -> device registry id of the car, filled in async_setup_entry
        self.car_device_ids: dict[str, str] = {}
        # first refused token refresh in a row, None while refreshes work
        self._refresh_failed_since: datetime | None = None

    async def _async_update_data(self) -> dict[str, VehicleData]:
        try:
            if self._vehicles is None:
                self._vehicles = await self.client.get_vehicles()
                if not self._vehicles:
                    raise UpdateFailed("No vehicle found on this Mazda account")

            result: dict[str, VehicleData] = {}
            for vehicle in self._vehicles:
                vid = vehicle.vehicle_id
                if vid not in self._functions:
                    try:
                        self._functions[vid] = await self.client.get_functions(vid)
                    except MazdaApiError as err:
                        _LOGGER.debug("No function config for %s: %s", vid, err)
                        self._functions[vid] = set()
                data = VehicleData(vehicle, await self.client.get_status(vid), self._functions[vid])
                if data.supports("#batteryScheduleHeating"):
                    try:
                        data.battery_preheat_plan = await self.client.get_battery_preheat_plan(vid)
                    except MazdaApiError as err:
                        _LOGGER.debug("No battery preheat plan for %s: %s", vid, err)
                result[vid] = data
            self._refresh_failed_since = None
            return result
        except MazdaRefreshError as err:
            now = dt_util.utcnow()
            if self._refresh_failed_since is None:
                self._refresh_failed_since = now
            # During setup there is no data to keep; HA would retry setup forever.
            if self.data is None or now - self._refresh_failed_since >= REFRESH_GRACE:
                raise ConfigEntryAuthFailed(str(err)) from err
            raise UpdateFailed(f"{err}; retrying before asking for a new login") from err
        except MazdaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MazdaError as err:
            raise UpdateFailed(str(err)) from err
