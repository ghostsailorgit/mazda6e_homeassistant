"""Remote lock entity and PIN options."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mazda6e.api import MazdaCommandError, MazdaPinError
from custom_components.mazda6e.const import (
    CONF_CONTROL_PIN,
    CONF_CONTROL_PRIVATE_KEY,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_STORE_PIN,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.mazda6e.models import Vehicle, VehicleStatus

CLIENT = "custom_components.mazda6e.api.Mazda6eClient"
LOCK = "lock.mazda_6e_doors"
VEHICLE = Vehicle(vehicle_id="42", vin="VIN0001", model_name="MAZDA 6e")
LOCKED = VehicleStatus.from_api({"door": {"driverLock": 0, "passengerLock": 0}})


async def _setup(hass: HomeAssistant, options=None, key="KEY") -> MockConfigEntry:
    data = {
        CONF_EMAIL: "a@b.c",
        CONF_REGION: "europe",
        CONF_DEVICE_ID: "dev",
        CONF_TOKEN: "t",
        CONF_REFRESH_TOKEN: "r",
    }
    if key:
        data[CONF_CONTROL_PRIVATE_KEY] = key
    entry = MockConfigEntry(domain=DOMAIN, unique_id="a@b.c", data=data, options=options or {})
    entry.add_to_hass(hass)
    with (
        patch(f"{CLIENT}.get_vehicles", AsyncMock(return_value=[VEHICLE])),
        patch(f"{CLIENT}.get_status", AsyncMock(return_value=LOCKED)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.fixture(autouse=True)
def status():
    with (
        patch(f"{CLIENT}.get_vehicles", AsyncMock(return_value=[VEHICLE])),
        patch(f"{CLIENT}.get_status", AsyncMock(return_value=LOCKED)),
    ):
        yield


async def test_no_lock_without_control_key(hass: HomeAssistant) -> None:
    await _setup(hass, key=None)
    assert hass.states.get(LOCK) is None


async def test_unlock_with_stored_pin(hass: HomeAssistant) -> None:
    await _setup(hass, options={CONF_CONTROL_PIN: "123456"})
    state = hass.states.get(LOCK)
    assert state.state == "locked"
    assert state.attributes.get("code_format") is None

    with patch(f"{CLIENT}.set_locked", AsyncMock()) as set_locked:
        await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    set_locked.assert_awaited_once_with("42", False, pin=None)
    # optimistic until the car reports the new state
    assert hass.states.get(LOCK).state == "unlocked"


async def test_pin_asked_when_not_stored(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert hass.states.get(LOCK).attributes["code_format"] == r"^\d{6}$"

    with patch(f"{CLIENT}.set_locked", AsyncMock()) as set_locked:
        await hass.services.async_call(
            "lock", "unlock", {"entity_id": LOCK, "code": "654321"}, blocking=True
        )
    set_locked.assert_awaited_once_with("42", False, pin="654321")


async def test_wrong_pin_error(hass: HomeAssistant) -> None:
    await _setup(hass, options={CONF_CONTROL_PIN: "123456"})
    with (
        patch(f"{CLIENT}.set_locked", AsyncMock(side_effect=MazdaPinError("no", 2))),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert err.value.translation_key == "invalid_pin_attempts"
    assert hass.states.get(LOCK).state == "locked"


async def test_command_failure_keeps_state(hass: HomeAssistant) -> None:
    await _setup(hass, options={CONF_CONTROL_PIN: "123456"})
    with (
        patch(f"{CLIENT}.set_locked", AsyncMock(side_effect=MazdaCommandError("timeout"))),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call("lock", "unlock", {"entity_id": LOCK}, blocking=True)
    assert hass.states.get(LOCK).state == "locked"


async def _options(hass, entry, user_input):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def test_options_store_verified_pin(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    with patch(f"{CLIENT}.get_rc_token", AsyncMock(return_value="rc")) as check:
        result = await _options(
            hass, entry, {CONF_SCAN_INTERVAL: 5, CONF_STORE_PIN: True, CONF_CONTROL_PIN: "123456"}
        )
    check.assert_awaited_once_with("123456")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CONTROL_PIN] == "123456"
    await hass.async_block_till_done()
    assert entry.runtime_data.client.control_pin == "123456"
    assert hass.states.get(LOCK).attributes.get("code_format") is None


async def test_options_reject_bad_pin(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    result = await _options(
        hass, entry, {CONF_SCAN_INTERVAL: 5, CONF_STORE_PIN: True, CONF_CONTROL_PIN: "12a"}
    )
    assert result["errors"] == {CONF_CONTROL_PIN: "invalid_pin_format"}

    with patch(f"{CLIENT}.get_rc_token", AsyncMock(side_effect=MazdaPinError("no", 3))):
        result = await _options(
            hass, entry, {CONF_SCAN_INTERVAL: 5, CONF_STORE_PIN: True, CONF_CONTROL_PIN: "999999"}
        )
    assert result["errors"] == {CONF_CONTROL_PIN: "invalid_pin_attempts"}
    assert CONF_CONTROL_PIN not in entry.options


async def test_options_keep_and_remove_pin(hass: HomeAssistant) -> None:
    entry = await _setup(hass, options={CONF_CONTROL_PIN: "123456"})
    result = await _options(hass, entry, {CONF_SCAN_INTERVAL: 7, CONF_STORE_PIN: True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 7, CONF_CONTROL_PIN: "123456"}

    await _options(hass, entry, {CONF_SCAN_INTERVAL: 7, CONF_STORE_PIN: False})
    assert CONF_CONTROL_PIN not in entry.options
    await hass.async_block_till_done()
    assert entry.runtime_data.client.control_pin is None
