"""Config flow and setup tests against a real Home Assistant core."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mazda6e.api import MazdaAuthError
from custom_components.mazda6e.const import (
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.mazda6e.models import Vehicle, VehicleStatus

CLIENT = "custom_components.mazda6e.api.Mazda6eClient"

STATUS = VehicleStatus.from_api(
    {
        "vehicleStatus": {"soc": 81, "drvMileage": 400, "status": 2},
        "charge": {"chargeStatus": 6, "chargeConStatus": 1, "chargeCurrent": 16},
        "door": {"doors": [0, 0, 0, 0], "trunk": 1, "driverLock": 0, "passengerLock": 0},
        "window": {"windows": [0, 0, 0, 0]},
    }
)
VEHICLE = Vehicle(vehicle_id="42", vin="VIN0001", model_name="MAZDA 6e")


async def test_config_flow_with_email_code(hass: HomeAssistant) -> None:
    with (
        patch(f"{CLIENT}.login", AsyncMock(return_value=True)) as login,
        patch(f"{CLIENT}.request_device_code", AsyncMock()) as request_code,
        patch(f"{CLIENT}.verify_device_code", AsyncMock()) as verify,
        patch("custom_components.mazda6e.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "Me@Example.com", CONF_PASSWORD: "pw", CONF_REGION: "europe"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "verify"
        login.assert_awaited_once()
        request_code.assert_awaited_once_with("Me@Example.com")

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
        verify.assert_awaited_once_with("Me@Example.com", "123456")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == "me@example.com"
    assert entry.data[CONF_DEVICE_ID]
    assert CONF_PASSWORD not in entry.data


async def test_config_flow_invalid_auth(hass: HomeAssistant) -> None:
    with patch(f"{CLIENT}.login", AsyncMock(side_effect=MazdaAuthError("no"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "a@b.c", CONF_PASSWORD: "bad", CONF_REGION: "europe"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="a@b.c",
        data={
            CONF_EMAIL: "a@b.c",
            CONF_REGION: "europe",
            CONF_DEVICE_ID: "dev",
            CONF_TOKEN: "t",
            CONF_REFRESH_TOKEN: "r",
        },
    )


async def test_setup_creates_entities(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{CLIENT}.get_vehicles", AsyncMock(return_value=[VEHICLE])),
        patch(f"{CLIENT}.get_status", AsyncMock(return_value=STATUS)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("sensor.mazda_6e_battery_level").state == "81"
    assert hass.states.get("sensor.mazda_6e_charging_current_sum_of_phases").state == "16.0"
    assert hass.states.get("sensor.mazda_6e_charging_status").state == "charging"
    assert hass.states.get("binary_sensor.mazda_6e_lock").state == "off"  # locked
    assert hass.states.get("binary_sensor.mazda_6e_trunk").state == "on"
    assert hass.states.get("binary_sensor.mazda_6e_doors").state == "on"
    assert hass.states.get("binary_sensor.mazda_6e_windows").state == "off"
    assert hass.states.get("binary_sensor.mazda_6e_charging").state == "on"


async def test_expired_login_starts_reauth(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.get_vehicles", AsyncMock(side_effect=MazdaAuthError("expired"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)


async def test_token_update_does_not_reload(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{CLIENT}.get_vehicles", AsyncMock(return_value=[VEHICLE])),
        patch(f"{CLIENT}.get_status", AsyncMock(return_value=STATUS)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator.client._on_token_update("t2", "r2")
        await hass.async_block_till_done()
        assert entry.data[CONF_TOKEN] == "t2"
        assert entry.runtime_data is coordinator

        hass.config_entries.async_update_entry(entry, options={CONF_SCAN_INTERVAL: 10})
        await hass.async_block_till_done()
        assert coordinator.update_interval.total_seconds() == 600
