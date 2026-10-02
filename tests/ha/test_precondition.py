"""Departure plans, pre-conditioning services and events."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigSubentryDataWithId,
)
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
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
    PLAN_ENABLED,
    PLAN_TEMPERATURE,
    PLAN_TIME,
    PLAN_VEHICLE,
    PLAN_WEEKDAYS,
    SUBENTRY_PLAN,
)
from custom_components.mazda6e.models import Vehicle, VehicleStatus
from custom_components.mazda6e.precondition import EVENT_PRECONDITIONING

CLIENT = "custom_components.mazda6e.api.Mazda6eClient"
VEHICLE = Vehicle(vehicle_id="42", vin="VIN0001", model_name="MAZDA 6e")
PLAN = {"planId": 7, "planType": 0, "isValid": 0, "endData": "20260928060000"}
MASTER = "switch.mazda_6e_pre_conditioning_departure_plans"
WEEKDAYS_MON_FRI = ["mon", "tue", "wed", "thu", "fri"]


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


def _plan(subentry_id, title, at, weekdays, temperature=21.0, enabled=True) -> ConfigSubentryDataWithId:
    return ConfigSubentryDataWithId(
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_PLAN,
        title=title,
        unique_id=None,
        data={
            PLAN_VEHICLE: "42",
            PLAN_TIME: at,
            PLAN_WEEKDAYS: list(weekdays),
            PLAN_TEMPERATURE: temperature,
            PLAN_ENABLED: enabled,
        },
    )


def _entry(*plans: ConfigSubentryDataWithId) -> MockConfigEntry:
    return MockConfigEntry(
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
        subentries_data=list(plans),
    )


async def _setup(hass: HomeAssistant, *plans: ConfigSubentryDataWithId) -> MockConfigEntry:
    entry = _entry(*plans)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _local(*args) -> datetime:
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


async def _call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
    await hass.async_block_till_done()  # update listener of the subentry


def _next_departure(hass) -> datetime:
    return dt_util.parse_datetime(hass.states.get("sensor.mazda_6e_next_departure").state)


async def test_next_departure_over_plans_and_skip(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))  # Monday 06:00
    await _setup(
        hass,
        _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI),
        _plan("gym", "Gym", "17:00", ["tue", "thu"], temperature=19.0),
    )
    assert hass.states.get("sensor.mazda_6e_next_departure").state == "unknown"

    await _call(hass, "switch", "turn_on", MASTER)
    assert _next_departure(hass) == _local(2026, 10, 5, 7, 30)
    start = hass.states.get("sensor.mazda_6e_next_pre_conditioning_start").state
    assert dt_util.parse_datetime(start) == _local(2026, 10, 5, 7, 15)
    assert hass.states.get("sensor.mazda_6e_next_departure").attributes["plan"] == "Work"

    # Work off -> Gym on Tuesday; its time can be changed on the dashboard
    await _call(hass, "switch", "turn_off", "switch.mazda_6e_work_departure_plan")
    await _call(hass, "time", "set_value", "time.mazda_6e_gym_departure_time", time="16:30")
    assert _next_departure(hass) == _local(2026, 10, 6, 16, 30)
    assert hass.states.get("sensor.mazda_6e_next_departure").attributes["plan"] == "Gym"

    # skip -> Thursday
    await _call(hass, "button", "press", "button.mazda_6e_skip_next_departure")
    assert _next_departure(hass) == _local(2026, 10, 8, 16, 30)


async def test_undo_skip(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))  # Monday 06:00
    await _setup(
        hass,
        _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI),
        _plan("gym", "Gym", "17:00", ["mon"], enabled=False),
    )
    undo = "button.mazda_6e_undo_skip"
    skip = "button.mazda_6e_skip_next_departure"
    await _call(hass, "switch", "turn_on", MASTER)
    assert hass.states.get(undo).state == "unavailable"  # nothing skipped

    # undo button
    await _call(hass, "button", "press", skip)
    assert _next_departure(hass) == _local(2026, 10, 6, 7, 30)
    assert hass.states.get(undo).state != "unavailable"
    await _call(hass, "button", "press", undo)
    assert _next_departure(hass) == _local(2026, 10, 5, 7, 30)
    assert hass.states.get(undo).state == "unavailable"

    # switching another plan on keeps the skip; switching its own plan on again undoes it
    await _call(hass, "button", "press", skip)
    await _call(hass, "switch", "turn_on", "switch.mazda_6e_gym_departure_plan")
    assert _next_departure(hass) == _local(2026, 10, 5, 17, 0)  # Work skipped, Gym next
    await _call(hass, "switch", "turn_off", "switch.mazda_6e_gym_departure_plan")
    await _call(hass, "switch", "turn_off", "switch.mazda_6e_work_departure_plan")
    await _call(hass, "switch", "turn_on", "switch.mazda_6e_work_departure_plan")
    assert _next_departure(hass) == _local(2026, 10, 5, 7, 30)

    # switching all plans on again undoes it too
    await _call(hass, "button", "press", skip)
    await _call(hass, "switch", "turn_off", MASTER)
    await _call(hass, "switch", "turn_on", MASTER)
    assert _next_departure(hass) == _local(2026, 10, 5, 7, 30)


async def test_schedule_runs_each_plan_with_its_temperature(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))
    await _setup(
        hass,
        _plan("work", "Work", "07:30", ["mon"], temperature=22.5),
        _plan("home", "Home", "17:00", ["mon"], temperature=19.0),
    )
    events = async_capture_events(hass, EVENT_PRECONDITIONING)
    await _call(hass, "select", "select_option", "select.mazda_6e_pre_conditioning_seat_heating", option="2")
    await _call(hass, "switch", "turn_on", "switch.mazda_6e_pre_conditioning_steering_wheel_heating")
    await _call(hass, "switch", "turn_on", MASTER)

    freezer.move_to(_local(2026, 10, 5, 7, 15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    client["set_climate"].assert_awaited_once_with("42", True, 22.5, run_time=15)
    client["set_seat"].assert_awaited_once_with("42", "heat", "driver", 2)
    client["set_steering_wheel_heat"].assert_awaited_once_with("42", True)
    client["set_defrost"].assert_not_awaited()
    assert events[-1].data["action"] == "started"
    assert events[-1].data["source"] == "schedule"
    assert events[-1].data["plan"] == "Work"
    assert events[-1].data["failed"] == []
    # the second plan of the same day is next
    assert _next_departure(hass) == _local(2026, 10, 5, 17, 0)

    freezer.move_to(_local(2026, 10, 5, 16, 45))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert client["set_climate"].await_args == call("42", True, 19.0, run_time=15)
    assert events[-1].data["plan"] == "Home"


async def test_plan_entities_store_in_subentry(hass: HomeAssistant, client) -> None:
    entry = await _setup(hass, _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI))
    await _call(hass, "time", "set_value", "time.mazda_6e_work_departure_time", time="08:10")
    await _call(hass, "number", "set_value", "number.mazda_6e_work_temperature", value=23.5)
    await _call(hass, "switch", "turn_off", "switch.mazda_6e_work_departure_plan")

    data = entry.subentries["work"].data
    assert (data[PLAN_TIME], data[PLAN_TEMPERATURE], data[PLAN_ENABLED]) == ("08:10", 23.5, False)
    assert hass.states.get("time.mazda_6e_work_departure_time").state == "08:10:00"
    assert hass.states.get("number.mazda_6e_work_temperature").state == "23.5"
    assert hass.states.get("switch.mazda_6e_work_departure_plan").state == "off"

    # each plan is its own device below the car, so the car stays outside the subentry
    entity = er.async_get(hass).async_get("switch.mazda_6e_work_departure_plan")
    assert entity.config_subentry_id == "work"
    devices = dr.async_get(hass)
    plan_device = devices.async_get(entity.device_id)
    car = devices.async_get(plan_device.via_device_id)
    assert plan_device.name == "MAZDA 6e Work"
    assert (DOMAIN, "VIN0001") in car.identifiers
    assert getattr(car, "config_subentry_id", None) is None


async def test_add_and_change_plan_in_dialog(hass: HomeAssistant, client) -> None:
    entry = await _setup(hass)
    flows = hass.config_entries.subentries

    result = await flows.async_init((entry.entry_id, SUBENTRY_PLAN), context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    form = {"name": "Gym", "time": "17:00:00", "weekdays": [], "temperature": 19}
    result = await flows.async_configure(result["flow_id"], form)
    assert result["errors"] == {"weekdays": "weekday_required"}
    result = await flows.async_configure(result["flow_id"], {**form, "weekdays": ["thu", "tue"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()  # reload for the new plan's entities

    subentry_id, subentry = next(iter(entry.subentries.items()))
    assert subentry.title == "Gym"
    assert dict(subentry.data) == {
        PLAN_TIME: "17:00",
        PLAN_WEEKDAYS: ["tue", "thu"],
        PLAN_TEMPERATURE: 19.0,
        PLAN_VEHICLE: "42",
        PLAN_ENABLED: True,
    }
    assert hass.states.get("switch.mazda_6e_gym_departure_plan").state == "on"

    result = await flows.async_init(
        (entry.entry_id, SUBENTRY_PLAN), context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id}
    )
    result = await flows.async_configure(
        result["flow_id"], {"name": "Gym", "time": "18:15:00", "weekdays": ["mon"], "temperature": 18.5}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.subentries[subentry_id].data[PLAN_TIME] == "18:15"
    assert entry.subentries[subentry_id].data[PLAN_WEEKDAYS] == ["mon"]
    assert hass.states.get("time.mazda_6e_gym_departure_time").state == "18:15:00"


async def test_weekly_plan_becomes_departure_plans(hass: HomeAssistant, client, hass_storage) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}.42.precondition"
    days = {day: {"on": day not in ("sat", "sun"), "time": "07:30"} for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    days["mon"] = {"on": False, "time": "06:30"}
    hass_storage[key] = {"version": 1, "key": key, "data": {"enabled": True, "temperature": 20.0, "days": days}}
    registry = er.async_get(hass)
    old = registry.async_get_or_create("switch", DOMAIN, "VIN0001_precondition_mon", config_entry=entry)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    plans = sorted((s.title, dict(s.data)) for s in entry.subentries.values())
    assert plans == [
        ("06:30", {PLAN_TIME: "06:30", PLAN_WEEKDAYS: ["mon"], PLAN_TEMPERATURE: 20.0, PLAN_ENABLED: False, PLAN_VEHICLE: "42"}),
        (
            "07:30",
            {PLAN_TIME: "07:30", PLAN_WEEKDAYS: ["tue", "wed", "thu", "fri"], PLAN_TEMPERATURE: 20.0, PLAN_ENABLED: True, PLAN_VEHICLE: "42"},
        ),
    ]
    assert "days" not in hass_storage[key]["data"]
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get(MASTER).state == "on"
    assert hass.states.get("switch.mazda_6e_07_30_departure_plan").state == "on"


async def test_unused_weekly_plan_is_not_migrated(hass: HomeAssistant, client, hass_storage) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}.42.precondition"
    days = {day: {"on": day not in ("sat", "sun"), "time": "07:30"} for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    hass_storage[key] = {"version": 1, "key": key, "data": {"enabled": False, "lead": 20, "days": days}}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not entry.subentries
    assert hass.states.get("number.mazda_6e_pre_conditioning_lead_time").state == "20"


async def test_battery_plan_follows_next_departure(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))
    await _setup(hass, _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI))
    await _call(hass, "switch", "turn_on", MASTER)
    client["set_battery_preheat"].assert_not_awaited()

    await _call(hass, "switch", "turn_on", "switch.mazda_6e_pre_conditioning_battery_preheating")
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
    assert events[0].data["plan"] is None


async def test_service_stop_and_without_device(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    await hass.services.async_call(DOMAIN, "stop_preconditioning", {}, blocking=True)
    client["set_climate"].assert_awaited_once_with("42", False, 21.0)


async def test_schedule_skipped_when_forecast_not_cold_enough(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))
    await _setup(hass, _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI))
    events = async_capture_events(hass, EVENT_PRECONDITIONING)
    hass.states.async_set("weather.test", "sunny")
    await _call(hass, "select", "select_option", "select.mazda_6e_pre_conditioning_weather_source", option="weather.test")
    await _call(hass, "number", "set_value", "number.mazda_6e_pre_conditioning_minimum_temperature", value=10)
    await _call(hass, "switch", "turn_on", MASTER)

    with patch(
        "custom_components.mazda6e.precondition.Preconditioner._async_forecast_temperature",
        AsyncMock(return_value=15.0),
    ):
        freezer.move_to(_local(2026, 10, 5, 7, 15))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    client["set_climate"].assert_not_awaited()
    assert events[-1].data["action"] == "skipped"
    assert events[-1].data["plan"] == "Work"
    # still reschedules for the next day
    assert _next_departure(hass) == _local(2026, 10, 6, 7, 30)


async def test_schedule_runs_when_forecast_is_cold(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))
    await _setup(hass, _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI))
    hass.states.async_set("weather.test", "snowy")
    await _call(hass, "select", "select_option", "select.mazda_6e_pre_conditioning_weather_source", option="weather.test")
    await _call(hass, "number", "set_value", "number.mazda_6e_pre_conditioning_minimum_temperature", value=10)
    await _call(hass, "switch", "turn_on", MASTER)

    with patch(
        "custom_components.mazda6e.precondition.Preconditioner._async_forecast_temperature",
        AsyncMock(return_value=2.0),
    ):
        freezer.move_to(_local(2026, 10, 5, 7, 15))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    client["set_climate"].assert_awaited_once_with("42", True, 21.0, run_time=15)


async def test_forecast_at_next_departure(hass: HomeAssistant, client, freezer) -> None:
    freezer.move_to(_local(2026, 10, 5, 6, 0))  # Monday 06:00
    await _setup(hass, _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI))
    sensor = "sensor.mazda_6e_forecast_at_departure"
    hours = [_local(2026, 10, 5, 6, 0) + timedelta(hours=h) for h in range(24)]
    forecast = [{"datetime": t.isoformat(), "temperature": 4.0 + i} for i, t in enumerate(hours)]
    daily = [
        {"datetime": _local(2026, 10, 5, 12, 0).isoformat(), "temperature": 14.0, "templow": 3.0},
        {"datetime": _local(2026, 10, 6, 12, 0).isoformat(), "temperature": 12.0, "templow": 1.5},
    ]

    async def get_forecasts(call: ServiceCall):
        return {"weather.test": {"forecast": forecast if call.data["type"] == "hourly" else daily}}

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    hass.states.async_set("weather.test", "cloudy")
    await _call(hass, "switch", "turn_on", MASTER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(sensor).state == "unknown"  # no weather source chosen

    await _call(hass, "select", "select_option", "select.mazda_6e_pre_conditioning_weather_source", option="weather.test")
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(sensor)
    assert float(state.state) == 5.0  # forecast hour 07:00, closest to 07:30
    assert state.attributes["forecast_type"] == "hourly"
    assert state.attributes["plan"] == "Work"

    # Tuesday 07:30 lies beyond the hourly forecast: the day's low instead of a far-off hour
    await _call(hass, "button", "press", "button.mazda_6e_skip_next_departure")
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(sensor)
    assert float(state.state) == 1.5
    assert state.attributes["forecast_type"] == "daily"

    # a newer forecast is fetched periodically
    forecast.extend(
        {"datetime": (hours[-1] + timedelta(hours=h)).isoformat(), "temperature": -3.0} for h in range(1, 30)
    )
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(sensor)
    assert float(state.state) == -3.0
    assert state.attributes["forecast_type"] == "hourly"

    # no forecast for the departure day at all
    daily.clear()
    forecast.clear()
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(sensor)
    assert state.state == "unknown"
    assert state.attributes["forecast_type"] is None


async def test_forecast_follows_late_weather_entity(hass: HomeAssistant, client, freezer) -> None:
    """A weather integration that starts after us still gets the forecast shown."""
    freezer.move_to(_local(2026, 10, 5, 6, 0))
    await _setup(hass, _plan("work", "Work", "07:30", WEEKDAYS_MON_FRI))
    sensor = "sensor.mazda_6e_forecast_at_departure"
    hass.states.async_set("weather.test", "cloudy")
    await _call(hass, "select", "select_option", "select.mazda_6e_pre_conditioning_weather_source", option="weather.test")
    await _call(hass, "switch", "turn_on", MASTER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(sensor).state == "unknown"  # weather service not there yet

    async def get_forecasts(call: ServiceCall):
        hourly = [{"datetime": _local(2026, 10, 5, 7, 0).isoformat(), "temperature": 2.5}]
        return {"weather.test": {"forecast": hourly}}

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    hass.states.async_set("weather.test", "snowy")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get(sensor).state) == 2.5


async def test_plan_button_reports_failed_steps(hass: HomeAssistant, client) -> None:
    await _setup(hass)
    client["set_climate"].side_effect = MazdaCommandError("offline")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.mazda_6e_start_pre_conditioning"}, blocking=True
        )
    assert err.value.translation_key == "precondition_failed"
    assert client["set_climate"].await_args == call("42", True, 21.0, run_time=15)
