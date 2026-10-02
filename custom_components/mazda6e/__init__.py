"""Home Assistant integration for the Mazda 6e / CX-6e."""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import Mazda6eClient
from .const import (
    CONF_CONTROL_PIN,
    CONF_CONTROL_PRIVATE_KEY,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLAN_VEHICLE,
    SUBENTRY_PLAN,
)
from .coordinator import Mazda6eConfigEntry, Mazda6eCoordinator
from .precondition import WEEKDAYS, Preconditioner, legacy_plan_data, plans_from_entry
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: Mazda6eConfigEntry) -> bool:
    def store_tokens(token: str, refresh_token: str) -> None:
        # Persist rotated tokens so a restart does not need a new login.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TOKEN: token, CONF_REFRESH_TOKEN: refresh_token},
        )

    client = Mazda6eClient(
        async_get_clientsession(hass),
        entry.data[CONF_REGION],
        entry.data[CONF_DEVICE_ID],
        token=entry.data[CONF_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        on_token_update=store_tokens,
        private_key=entry.data.get(CONF_CONTROL_PRIVATE_KEY),
        control_pin=entry.options.get(CONF_CONTROL_PIN),
    )
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = Mazda6eCoordinator(hass, entry, client, timedelta(minutes=minutes))
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    if entry.data.get(CONF_CONTROL_PRIVATE_KEY):
        for vehicle_id in coordinator.data:
            preconditioner = Preconditioner(
                hass, coordinator, vehicle_id, lambda vid=vehicle_id: plans_from_entry(entry, vid)
            )
            await preconditioner.async_load()
            await _async_migrate_weekly_plan(hass, entry, preconditioner)
            coordinator.preconditioners[vehicle_id] = preconditioner
    coordinator.plan_titles = _plan_titles(entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


def _plan_titles(entry: Mazda6eConfigEntry) -> dict[str, str]:
    return {
        subentry_id: subentry.title
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_PLAN
    }


async def _async_migrate_weekly_plan(
    hass: HomeAssistant, entry: Mazda6eConfigEntry, preconditioner: Preconditioner
) -> None:
    """Turn the per-weekday plan of versions before 0.8 into departure plans."""
    if preconditioner.legacy_days is not None:
        temperature = preconditioner.settings["temperature"]
        for title, data in legacy_plan_data(preconditioner.legacy_days, temperature):
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({**data, PLAN_VEHICLE: preconditioner.vehicle_id}),
                    subentry_type=SUBENTRY_PLAN,
                    title=title,
                    unique_id=None,
                ),
            )
        await preconditioner.async_forget_legacy_days()
        preconditioner.async_plans_changed()

    # The weekday switches and departure times of the old plan are gone.
    registry = er.async_get(hass)
    vin = preconditioner.coordinator.data[preconditioner.vehicle_id].vehicle.vin
    for domain, key in [
        *(("switch", f"precondition_{day}") for day in WEEKDAYS),
        *(("time", f"precondition_departure_{day}") for day in WEEKDAYS),
    ]:
        if entity_id := registry.async_get_entity_id(domain, DOMAIN, f"{vin}_{key}"):
            registry.async_remove(entity_id)


async def _async_entry_updated(hass: HomeAssistant, entry: Mazda6eConfigEntry) -> None:
    # Called for token updates too, so apply options in place instead of reloading.
    coordinator = entry.runtime_data
    if _plan_titles(entry) != coordinator.plan_titles:
        # A departure plan was added, removed or renamed: its entities change.
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    for preconditioner in coordinator.preconditioners.values():
        preconditioner.async_plans_changed()
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator.update_interval = timedelta(minutes=minutes)
    pin = entry.options.get(CONF_CONTROL_PIN)
    if coordinator.client.control_pin != pin:
        coordinator.client.control_pin = pin
        # The lock's code_format depends on the PIN, so write its state again.
        coordinator.async_update_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: Mazda6eConfigEntry) -> bool:
    for preconditioner in entry.runtime_data.preconditioners.values():
        await preconditioner.async_unload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
