"""Remote climate entity."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mazda6e.api import MazdaCommandError
from custom_components.mazda6e.const import (
    CONF_CONTROL_PRIVATE_KEY,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.mazda6e.models import Vehicle, VehicleStatus

CLIENT = "custom_components.mazda6e.api.Mazda6eClient"
CLIMATE = "climate.mazda_6e_climate"
VEHICLE = Vehicle(vehicle_id="42", vin="VIN0001", model_name="MAZDA 6e")
OFF = VehicleStatus.from_api({"hvac": {"acStatus": 0, "remoteTemp": 0, "insideTemp": 123}})


@pytest.fixture(autouse=True)
def status():
    with (
        patch(f"{CLIENT}.get_vehicles", AsyncMock(return_value=[VEHICLE])),
        patch(f"{CLIENT}.get_status", AsyncMock(return_value=OFF)),
    ):
        yield


async def _setup(hass: HomeAssistant, key: str | None = "KEY") -> MockConfigEntry:
    data = {
        CONF_EMAIL: "a@b.c",
        CONF_REGION: "europe",
        CONF_DEVICE_ID: "dev",
        CONF_TOKEN: "t",
        CONF_REFRESH_TOKEN: "r",
    }
    if key:
        data[CONF_CONTROL_PRIVATE_KEY] = key
    entry = MockConfigEntry(domain=DOMAIN, unique_id="a@b.c", data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_no_climate_without_control_key(hass: HomeAssistant) -> None:
    await _setup(hass, key=None)
    assert hass.states.get(CLIMATE) is None


async def test_state_from_car(hass: HomeAssistant) -> None:
    await _setup(hass)
    state = hass.states.get(CLIMATE)
    assert state.state == "off"
    assert state.attributes["current_temperature"] == 12.3
    assert state.attributes["temperature"] == 21.0  # default while off


async def test_turn_on_with_temperature(hass: HomeAssistant) -> None:
    await _setup(hass)
    with patch(f"{CLIENT}.set_climate", AsyncMock()) as set_climate:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": CLIMATE, "temperature": 23.5, "hvac_mode": "heat_cool"},
            blocking=True,
        )
    set_climate.assert_awaited_once_with("42", True, 23.5)
    state = hass.states.get(CLIMATE)
    assert state.state == "heat_cool"  # optimistic until the car reports
    assert state.attributes["temperature"] == 23.5


async def test_temperature_while_off_does_not_start(hass: HomeAssistant) -> None:
    await _setup(hass)
    with patch(f"{CLIENT}.set_climate", AsyncMock()) as set_climate:
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 19}, blocking=True
        )
        set_climate.assert_not_awaited()
        assert hass.states.get(CLIMATE).attributes["temperature"] == 19

        await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    set_climate.assert_awaited_once_with("42", True, 19.0)


async def test_turn_off(hass: HomeAssistant) -> None:
    await _setup(hass)
    with patch(f"{CLIENT}.set_climate", AsyncMock()) as set_climate:
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "off"}, blocking=True
        )
    set_climate.assert_awaited_once_with("42", False, 21.0)


async def test_failure_keeps_state(hass: HomeAssistant) -> None:
    await _setup(hass)
    with (
        patch(f"{CLIENT}.set_climate", AsyncMock(side_effect=MazdaCommandError("timeout"))),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call("climate", "turn_on", {"entity_id": CLIMATE}, blocking=True)
    assert err.value.translation_key == "command_failed"
    assert hass.states.get(CLIMATE).state == "off"
