"""Home Assistant integration for the Mazda 6e / CX-6e."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Mazda6eClient
from .const import (
    CONF_CONTROL_PIN,
    CONF_CONTROL_PRIVATE_KEY,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import Mazda6eConfigEntry, Mazda6eCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER, Platform.LOCK, Platform.SENSOR]


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
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: Mazda6eConfigEntry) -> None:
    # Called for token updates too, so apply options in place instead of reloading.
    coordinator = entry.runtime_data
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator.update_interval = timedelta(minutes=minutes)
    pin = entry.options.get(CONF_CONTROL_PIN)
    if coordinator.client.control_pin != pin:
        coordinator.client.control_pin = pin
        # The lock's code_format depends on the PIN, so write its state again.
        coordinator.async_update_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: Mazda6eConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
