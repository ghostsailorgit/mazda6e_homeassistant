"""Direct remote controls: buttons, number, switches, selects, covers, times."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.mazda6e.api import FLASH_AND_HONK, FLASH_ONLY, MazdaPinError
from custom_components.mazda6e.button import STATUS_UPLOAD_DELAY
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
VEHICLE = Vehicle(vehicle_id="42", vin="VIN0001", model_name="MAZDA 6e")
# As a real car reports it: no time zone or time format.
CHARGE_PLAN = {
    "planId": 5,
    "planType": 1,
    "isValid": 1,
    "startTime": "2300",
    "startSwitch": 1,
    "endTime": "0600",
    "endSwitch": 1,
}
COMMANDS = (
    "request_status_update",
    "flash_and_honk",
    "set_charge_limit",
    "set_seat",
    "set_steering_wheel_heat",
    "set_defrost",
    "set_windows",
    "set_trunk",
    "set_battery_preheat",
    "set_charge_plan",
    "set_charge_plan_enabled",
)


def _status() -> VehicleStatus:
    return VehicleStatus.from_api(
        {
            "charge": {"maxSocPercent": 80, "chargePlanList": [dict(CHARGE_PLAN)]},
            "seat": {"leftFront": {"heatStatus": 0, "ventStatus": 0}},
            "hvac": {"defrostStatus": 0},
            "window": {"windows": [0, 0, 0, 0]},
            "door": {"trunk": 0},
        }
    )


@pytest.fixture
def client():
    mocks = {name: AsyncMock() for name in COMMANDS}
    with (
        patch(f"{CLIENT}.get_vehicles", AsyncMock(return_value=[VEHICLE])),
        patch(f"{CLIENT}.get_status", AsyncMock(side_effect=lambda _v: _status())),
        patch(
            f"{CLIENT}.get_battery_preheat_plan",
            AsyncMock(return_value={"planId": 7, "planType": 0, "isValid": 0, "endData": "20260928060000"}),
        ),
        patch.multiple(CLIENT, **mocks),
    ):
        yield mocks


async def _setup(hass: HomeAssistant, functions: set[str] | None = None) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a@b.c",
        data={
            CONF_EMAIL: "a@b.c",
            CONF_REGION: "europe",
            CONF_DEVICE_ID: "dev",
            CONF_TOKEN: "t",
            CONF_REFRESH_TOKEN: "r",
            CONF_CONTROL_PRIVATE_KEY: "KEY",
        },
    )
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.get_functions", AsyncMock(return_value=functions or set())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def _call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


async def test_buttons(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    await _call(hass, "button", "press", "button.mazda_6e_request_status_update")
    client["request_status_update"].assert_awaited_once_with("42")
    async_fire_time_changed(hass, dt_util.utcnow() + STATUS_UPLOAD_DELAY)
    await hass.async_block_till_done()
    await _call(hass, "button", "press", "button.mazda_6e_flash_lights")
    await _call(hass, "button", "press", "button.mazda_6e_flash_and_honk")
    assert [c.args for c in client["flash_and_honk"].await_args_list] == [("42", FLASH_ONLY), ("42", FLASH_AND_HONK)]


async def test_charge_limit(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    assert hass.states.get("number.mazda_6e_set_charge_limit").state == "80"
    await _call(hass, "number", "set_value", "number.mazda_6e_set_charge_limit", value=90)
    client["set_charge_limit"].assert_awaited_once_with("42", 90)
    assert hass.states.get("number.mazda_6e_set_charge_limit").state == "90"


async def test_seat_and_switches(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    await _call(hass, "select", "select_option", "select.mazda_6e_seat_heating_driver", option="3")
    await _call(hass, "select", "select_option", "select.mazda_6e_seat_ventilation_passenger", option="off")
    assert [c.args for c in client["set_seat"].await_args_list] == [
        ("42", "heat", "driver", 3),
        ("42", "wind", "passenger", 0),
    ]
    assert hass.states.get("select.mazda_6e_seat_heating_driver").state == "3"

    await _call(hass, "switch", "turn_on", "switch.mazda_6e_defrost_windscreen")
    client["set_defrost"].assert_awaited_once_with("42", True)
    await _call(hass, "switch", "turn_on", "switch.mazda_6e_steering_wheel_heating")
    client["set_steering_wheel_heat"].assert_awaited_once_with("42", True)


async def test_seats_follow_function_config(hass: HomeAssistant, client) -> None:
    await _setup(hass, functions={"#driverSeatHeat", "ACSW"})
    assert hass.states.get("select.mazda_6e_seat_heating_driver") is not None
    assert hass.states.get("select.mazda_6e_seat_heating_passenger") is None
    assert hass.states.get("select.mazda_6e_seat_ventilation_driver") is None
    assert hass.states.get("button.mazda_6e_flash_and_honk") is None  # no #findCar


async def test_covers_need_pin(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    assert hass.states.get("cover.mazda_6e_windows").state == "closed"
    await _call(hass, "cover", "open_cover", "cover.mazda_6e_windows")
    client["set_windows"].assert_awaited_once_with("42", True)
    assert hass.states.get("cover.mazda_6e_windows").state == "open"

    client["set_trunk"].side_effect = MazdaPinError("no pin")
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "cover", "open_cover", "cover.mazda_6e_tailgate")
    assert err.value.translation_key == "pin_missing"
    assert hass.states.get("cover.mazda_6e_tailgate").state == "closed"


async def test_car_plans(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    await _call(hass, "time", "set_value", "time.mazda_6e_charging_schedule_start", time="22:30")
    _, plan = client["set_charge_plan"].await_args.args
    assert plan["planId"] == 5
    assert client["set_charge_plan"].await_args.kwargs == {"start": "2230", "end": "0600"}

    assert hass.states.get("switch.mazda_6e_charging_schedule_car_plan").state == "on"
    await _call(hass, "switch", "turn_off", "switch.mazda_6e_charging_schedule_car_plan")
    client["set_charge_plan_enabled"].assert_awaited_once_with("42", 5, False)
    assert hass.states.get("switch.mazda_6e_charging_schedule_car_plan").state == "off"

    await _call(hass, "switch", "turn_on", "switch.mazda_6e_battery_preheating_car_plan")
    vid, plan, end = client["set_battery_preheat"].await_args.args
    assert (vid, plan["planId"], end) == ("42", 7, "20260928060000")
    await _call(hass, "switch", "turn_off", "switch.mazda_6e_battery_preheating_car_plan")
    assert client["set_battery_preheat"].await_args.args[2] is None
