"""Remote door lock for the Mazda 6e."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.lock import ATTR_CODE, LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import MazdaAuthError, MazdaError, MazdaPinError
from .const import CONF_CONTROL_PRIVATE_KEY, DOMAIN
from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eEntity

DOORS = EntityDescription(key="door_lock", translation_key="door_lock")

# The car often reports the new lock state only minutes later; show the
# commanded state until a newer report arrives or this time has passed.
OPTIMISTIC_HOLD = timedelta(minutes=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.data.get(CONF_CONTROL_PRIVATE_KEY):
        return
    coordinator = entry.runtime_data
    async_add_entities(Mazda6eLock(coordinator, vehicle_id, DOORS) for vehicle_id in coordinator.data)


class Mazda6eLock(Mazda6eEntity, LockEntity):
    _optimistic: bool | None = None
    _commanded_at: datetime | None = None

    @property
    def code_format(self) -> str | None:
        # Without a stored PIN, Home Assistant asks for it on every action.
        return None if self.coordinator.client.control_pin else r"^\d{6}$"

    @property
    def is_locked(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        return self.status.locked

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
            await self.coordinator.client.set_locked(self._vehicle_id, locked, pin=code or None)
        except MazdaPinError as err:
            if err.attempts_left is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_pin_attempts",
                    translation_placeholders={"attempts": str(err.attempts_left)},
                ) from err
            key = "pin_missing" if not (code or self.coordinator.client.control_pin) else "invalid_pin"
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
            self._optimistic = locked
            self._commanded_at = dt_util.utcnow()
        finally:
            self._attr_is_locking = False
            self._attr_is_unlocking = False
            self.async_write_ha_state()

        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None and self._commanded_at is not None:
            reported = self.status.last_update
            if (
                self.status.locked == self._optimistic
                or (reported is not None and reported > self._commanded_at)
                or dt_util.utcnow() - self._commanded_at > OPTIMISTIC_HOLD
            ):
                self._optimistic = None
                self._commanded_at = None
        super()._handle_coordinator_update()
