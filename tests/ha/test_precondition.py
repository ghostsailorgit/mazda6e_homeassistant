"""Weekly pre-conditioning plan, services and events."""

from datetime import datetime
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

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
from custom_components.mazda6e.precondition import EVENT_PRECONDITIONING

CLIENT = "custom_components.mazda6e.api.Mazda6eClient"
VEHICLE = Vehicle(vehicle_id="42", vin="VIN0001", model_name="MAZDA 6e")
PLAN = {"planId": 7, "planType": 0, "isValid": 0, "endData": "20260928060000"}


@pytest.fixture
def client():
    """All command methods of the client, mocked."""
    methods = (
        "set_climate",
        "set_seat",
        "set_steering_wheel_heat",
        "set_defrost",
        "set_battery_preheat",
    )
    mocks = {name: AsyncMock() for name in methods}
    with (
        patch(f"{CLIENT}.get_vehicles", AsyncMock(return_value=[VEHICLE])),
        patch(f"{CLIENT}.get_status", AsyncMock(return_value=VehicleStatus())),
        patch(f"{CLIENT}.get_battery_preheat_plan", AsyncMock(side_effect=lambda _v: dict(PLAN))),
        patch.multiple(CLIENT, **mocks),
    ):
        yield mocks


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
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
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _local(*args) -> datetime:
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


async def _turn_on(hass, entity_id):
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)


async def test_next_departure_and_skip(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))  # Monday 06:00
    entry = await _setup(hass)
    assert hass.states.get("sensor.mazda_6e_next_departure").state == "unknown"

    await _turn_on(hass, "switch.mazda_6e_pre_conditioning_weekly_plan")
    departure = hass.states.get("sensor.mazda_6e_next_departure").state
    start = hass.states.get("sensor.mazda_6e_next_pre_conditioning_start").state
    assert dt_util.parse_datetime(departure) == _local(2026, 10, 5, 7, 30)
    assert dt_util.parse_datetime(start) == _local(2026, 10, 5, 7, 15)

    # Monday off -> Tuesday; departure time change is used
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.mazda_6e_pre_conditioning_monday"}, blocking=True
    )
    await hass.services.async_call(
        "time", "set_value", {"entity_id": "time.mazda_6e_departure_tuesday", "time": "06:45"}, blocking=True
    )
    assert dt_util.parse_datetime(hass.states.get("sensor.mazda_6e_next_departure").state) == _local(
        2026, 10, 6, 6, 45
    )

    # skip -> Wednesday
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.mazda_6e_skip_next_departure"}, blocking=True
    )
    assert dt_util.parse_datetime(hass.states.get("sensor.mazda_6e_next_departure").state) == _local(
        2026, 10, 7, 7, 30
    )
    # weekend is off by default, Monday was switched off above -> Tuesday
    freezer.move_to(_local(2026, 10, 9, 8, 0))  # Friday after departure
    preconditioner = entry.runtime_data.preconditioners["42"]
    assert preconditioner.next_departure() == _local(2026, 10, 13, 6, 45)


async def test_schedule_runs_profile(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))
    await _setup(hass)
    events = async_capture_events(hass, EVENT_PRECONDITIONING)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.mazda_6e_pre_conditioning_seat_heating", "option": "2"},
        blocking=True,
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.mazda_6e_pre_conditioning_temperature", "value": 22.5},
        blocking=True,
    )
    await _turn_on(hass, "switch.mazda_6e_pre_conditioning_steering_wheel_heating")
    await _turn_on(hass, "switch.mazda_6e_pre_conditioning_weekly_plan")

    freezer.move_to(_local(2026, 10, 5, 7, 15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    client["set_climate"].assert_awaited_once_with("42", True, 22.5, run_time=15)
    client["set_seat"].assert_awaited_once_with("42", "heat", "driver", 2)
    client["set_steering_wheel_heat"].assert_awaited_once_with("42", True)
    client["set_defrost"].assert_not_awaited()
    assert events[-1].data["action"] == "started"
    assert events[-1].data["source"] == "schedule"
    assert events[-1].data["failed"] == []
    # next one is Tuesday
    assert dt_util.parse_datetime(hass.states.get("sensor.mazda_6e_next_departure").state) == _local(
        2026, 10, 6, 7, 30
    )


async def test_battery_plan_follows_next_departure(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))
    await _setup(hass)
    await _turn_on(hass, "switch.mazda_6e_pre_conditioning_weekly_plan")
    client["set_battery_preheat"].assert_not_awaited()

    await _turn_on(hass, "switch.mazda_6e_pre_conditioning_battery_preheating")
    await hass.async_block_till_done()
    vid, plan, end = client["set_battery_preheat"].await_args.args
    assert (vid, plan["planId"], end) == ("42", 7, "20261005073000")


async def test_service_start_with_overrides(hass: HomeAssistant, client) -> None:
    entry = await _setup(hass)
    client["set_defrost"].side_effect = MazdaCommandError("no")
    events = async_capture_events(hass, EVENT_PRECONDITIONING)
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]

    response = await hass.services.async_call(
        DOMAIN,
        "start_preconditioning",
        {"device_id": device.id, "temperature": 19, "duration": 20, "defrost": True, "seat_heat": 0},
        blocking=True,
        return_response=True,
    )
    assert response == {"failed": ["defrost"]}
    client["set_climate"].assert_awaited_once_with("42", True, 19.0, run_time=20)
    client["set_seat"].assert_not_awaited()
    assert events[0].data["source"] == "service"


async def test_service_stop_and_without_device(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    await hass.services.async_call(DOMAIN, "stop_preconditioning", {}, blocking=True)
    client["set_climate"].assert_awaited_once_with("42", False, 21.0)


async def test_settings_are_stored(hass: HomeAssistant, client, hass_storage) -> None:
    entry = await _setup(hass)
    await hass.services.async_call(
        "time", "set_value", {"entity_id": "time.mazda_6e_departure_friday", "time": "08:10"}, blocking=True
    )
    key = f"{DOMAIN}.{entry.entry_id}.42.precondition"
    assert hass_storage[key]["data"]["days"]["fri"] == {"on": True, "time": "08:10"}


async def test_plan_button_reports_failed_steps(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    client["set_climate"].side_effect = MazdaCommandError("offline")
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.mazda_6e_start_pre_conditioning"}, blocking=True
        )
    assert err.value.translation_key == "precondition_failed"
    assert client["set_climate"].await_args == call("42", True, 21.0, run_time=15)
