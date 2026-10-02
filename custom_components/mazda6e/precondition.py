"""Pre-conditioning: heat or cool the car before departure.

One ``Preconditioner`` per car keeps a profile (seat heating, steering wheel
heating, defrost, battery preheating, lead time) and runs it ``lead``
minutes before the next departure of its departure plans. Departure plans
are config subentries (name, time, weekdays, temperature, on/off), so users
add as many as they need from the integration page. The same profile can be
started from outside through the ``mazda6e.start_preconditioning`` service.

Settings are kept in Home Assistant, not on the car, so the plans work
without any app-side schedule.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import CLIMATE_MAX_TEMP, CLIMATE_MIN_TEMP, MazdaError
from .const import (
    DOMAIN,
    PLAN_ENABLED,
    PLAN_TEMPERATURE,
    PLAN_TIME,
    PLAN_VEHICLE,
    PLAN_WEEKDAYS,
    SUBENTRY_PLAN,
)

if TYPE_CHECKING:
    from .coordinator import Mazda6eCoordinator

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

EVENT_PRECONDITIONING = f"{DOMAIN}_preconditioning"

LEAD_MIN, LEAD_MAX = 5, 30

# How often the forecast for the next departure is fetched again, and how far
# the closest forecast hour may lie from the departure to still count.
FORECAST_REFRESH = timedelta(minutes=30)
FORECAST_MAX_GAP = timedelta(minutes=90)
STORAGE_VERSION = 1

# The single weekly plan of versions before 0.8, as it was stored by default.
LEGACY_DAYS: dict[str, dict[str, Any]] = {
    day: {"on": day not in ("sat", "sun"), "time": "07:30"} for day in WEEKDAYS
}

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "lead": 15,
    "temperature": 21.0,
    "seat_heat": 0,
    "steering_wheel": False,
    "defrost": False,
    "battery": False,
    "skip": None,  # ISO departure the user chose to skip
    "weather_entity": None,  # weather.* entity used to gate the schedule; None = always run
    "cold_below": 10.0,  # only auto-run if the forecast at departure is below this (°C)
}

NO_WEATHER_ENTITY = "none"


def parse_time(value: str) -> time:
    hour, minute = value.split(":")[:2]
    return time(int(hour), int(minute))


@dataclass(frozen=True)
class DeparturePlan:
    """One departure plan of a car, from a config subentry."""

    plan_id: str
    name: str
    time: time
    weekdays: frozenset[str]
    temperature: float
    enabled: bool


def plans_from_entry(entry: ConfigEntry, vehicle_id: str) -> list[DeparturePlan]:
    """The departure plans of one car, earliest departure time first."""
    plans = [
        DeparturePlan(
            plan_id=subentry.subentry_id,
            name=subentry.title,
            time=parse_time(subentry.data[PLAN_TIME]),
            weekdays=frozenset(subentry.data[PLAN_WEEKDAYS]),
            temperature=float(subentry.data[PLAN_TEMPERATURE]),
            enabled=bool(subentry.data[PLAN_ENABLED]),
        )
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_PLAN and subentry.data.get(PLAN_VEHICLE) == vehicle_id
    ]
    return sorted(plans, key=lambda plan: plan.time)


def legacy_plan_data(days: dict[str, Any], temperature: float) -> list[tuple[str, dict[str, Any]]]:
    """Turn the per-weekday plan of versions before 0.8 into departure plans.

    Days with the same time and on/off state become one plan; days still on
    their untouched default (07:30, off) are dropped. Returns (title, data)
    pairs without the vehicle id.
    """
    groups: dict[tuple[str, bool], list[str]] = {}
    for day in WEEKDAYS:
        setting = {**LEGACY_DAYS[day], **(days.get(day) or {})}
        on, at = bool(setting["on"]), str(setting["time"])[:5]
        if not on and at == "07:30":
            continue
        groups.setdefault((at, on), []).append(day)
    return [
        (
            at,
            {
                PLAN_TIME: at,
                PLAN_WEEKDAYS: weekdays,
                PLAN_TEMPERATURE: temperature,
                PLAN_ENABLED: on,
            },
        )
        for (at, on), weekdays in sorted(groups.items())
    ]


class Preconditioner:
    """Profile, departure plans and execution for one car."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: Mazda6eCoordinator,
        vehicle_id: str,
        plans: Callable[[], list[DeparturePlan]],
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.vehicle_id = vehicle_id
        self._plans = plans
        entry_id = coordinator.config_entry.entry_id
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.{vehicle_id}.precondition"
        )
        self.settings: dict[str, Any] = deepcopy(DEFAULTS)
        self.legacy_days: dict[str, Any] | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []
        self.last_run: dict[str, Any] | None = None
        self._running = asyncio.Lock()
        self._scheduled: tuple[datetime, DeparturePlan] | None = None
        self._unsub_forecast: CALLBACK_TYPE | None = None
        # Forecast temperature (°C) at the next departure and whether it came from
        # the "hourly" forecast or, for departures beyond it, the "daily" low.
        self.departure_forecast: float | None = None
        self.departure_forecast_type: str | None = None

    # ------------------------------------------------------------ settings

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        for key, value in stored.items():
            if key in DEFAULTS:
                self.settings[key] = value
        days = stored.get("days")
        if isinstance(days, dict) and (stored.get("enabled") or days != LEGACY_DAYS):
            self.legacy_days = days
        self._unsub_forecast = async_track_time_interval(
            self.hass, self._async_forecast_interval, FORECAST_REFRESH
        )
        self._schedule()

    async def async_forget_legacy_days(self) -> None:
        """Drop the pre-0.8 weekly plan once it was turned into departure plans."""
        self.legacy_days = None
        await self._store.async_save(self.settings)

    async def async_update(self, **changes: Any) -> None:
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise ValueError(f"Unknown setting {key}")
            self.settings[key] = value
        await self._store.async_save(self.settings)
        self._schedule()

    @callback
    def async_plans_changed(self) -> None:
        """A departure plan was switched or edited; plan again."""
        self._schedule()

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)

        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # ------------------------------------------------------------ planning

    def next_slot(
        self, now: datetime | None = None, *, include_skipped: bool = False
    ) -> tuple[datetime, DeparturePlan] | None:
        """Next departure (and its plan) whose start time still lies in the future."""
        if not self.settings["enabled"]:
            return None
        now = now or dt_util.now()
        lead = timedelta(minutes=self.settings["lead"])
        plans = [plan for plan in self._plans() if plan.enabled]
        for offset in range(8):
            date = (now + timedelta(days=offset)).date()
            weekday = WEEKDAYS[date.weekday()]
            for plan in plans:  # earliest time first
                if weekday not in plan.weekdays:
                    continue
                departure = datetime.combine(date, plan.time, tzinfo=now.tzinfo)
                if departure - lead <= now:
                    continue
                if not include_skipped and self.settings["skip"] == departure.isoformat():
                    continue
                return departure, plan
        return None

    def next_departure(self, now: datetime | None = None, *, include_skipped: bool = False) -> datetime | None:
        slot = self.next_slot(now, include_skipped=include_skipped)
        return slot[0] if slot else None

    def next_start(self, now: datetime | None = None) -> datetime | None:
        departure = self.next_departure(now)
        if departure is None:
            return None
        return departure - timedelta(minutes=self.settings["lead"])

    @callback
    def _schedule(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        self._scheduled = self.next_slot()
        departure = self._scheduled[0] if self._scheduled else None
        if departure is not None:
            start = departure - timedelta(minutes=self.settings["lead"])
            self._unsub_timer = async_track_point_in_time(self.hass, self._async_timer, start)
        self._sync_battery_plan(departure)
        self._refresh_forecast()
        self._notify()

    @callback
    def _async_forecast_interval(self, _now: datetime) -> None:
        self._refresh_forecast()

    @callback
    def _refresh_forecast(self) -> None:
        """Fetch the forecast temperature for the next departure in the background."""
        departure = self._scheduled[0] if self._scheduled else None
        entity_id = self.settings.get("weather_entity")
        if departure is None or not entity_id:
            self._set_departure_forecast(None, None)
            return

        async def refresh() -> None:
            forecast_type = "hourly"
            temperature = await self._async_forecast_temperature(entity_id, departure)
            if temperature is None:
                forecast_type = "daily"
                temperature = await self._async_daily_low(entity_id, departure)
            current = self._scheduled[0] if self._scheduled else None
            if current != departure or self.settings.get("weather_entity") != entity_id:
                return  # plans changed meanwhile; a newer refresh is on its way
            self._set_departure_forecast(temperature, forecast_type if temperature is not None else None)

        self.hass.async_create_background_task(refresh(), f"{DOMAIN} departure forecast")

    async def _async_timer(self, _now: datetime) -> None:
        self._unsub_timer = None
        if self._scheduled is None:
            return
        departure, plan = self._scheduled
        if not await self._should_preheat(departure):
            self._record("skipped", "schedule", departure, [], plan=plan.name)
            self._schedule()
            return
        # Battery preheating was already handed to the car when planning.
        await self.async_start(
            source="schedule",
            departure=departure,
            plan=plan.name,
            battery=False,
            temperature=plan.temperature,
        )
        self._schedule()

    async def _should_preheat(self, departure: datetime) -> bool:
        """Whether the weather gate allows the scheduled run to go ahead.

        No weather entity configured -> always run (old behaviour). A forecast
        that could not be read (service error, no data for that time) also
        lets the run go ahead, so a temporary weather-integration hiccup never
        silently skips pre-conditioning.
        """
        entity_id = self.settings.get("weather_entity")
        threshold = self.settings.get("cold_below")
        if not entity_id or threshold is None:
            return True
        temperature = await self._async_forecast_temperature(entity_id, departure)
        if temperature is None:
            return True
        return temperature < threshold

    @callback
    def _set_departure_forecast(self, temperature: float | None, forecast_type: str | None) -> None:
        if (temperature, forecast_type) != (self.departure_forecast, self.departure_forecast_type):
            self.departure_forecast, self.departure_forecast_type = temperature, forecast_type
            self._notify()

    async def _async_forecast(self, entity_id: str, forecast_type: str) -> list[dict[str, Any]]:
        """Forecast entries of one type; empty if the weather entity cannot deliver it."""
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": forecast_type},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - a weather hiccup must not break the schedule loop
            _LOGGER.warning("Could not fetch %s forecast from %s", forecast_type, entity_id, exc_info=True)
            return []
        return ((response or {}).get(entity_id) or {}).get("forecast") or []

    async def _async_daily_low(self, entity_id: str, when: datetime) -> float | None:
        """Lowest forecast temperature of the departure day (daily forecast)."""
        day = dt_util.as_local(when).date()
        for entry in await self._async_forecast(entity_id, "daily"):
            start = dt_util.parse_datetime(entry["datetime"])
            if start is None or dt_util.as_local(start).date() != day:
                continue
            temperature = entry.get("templow", entry.get("temperature"))
            return float(temperature) if temperature is not None else None
        return None

    async def _async_forecast_temperature(self, entity_id: str, when: datetime) -> float | None:
        """Forecast temperature closest to ``when`` from an hourly forecast."""
        forecast = await self._async_forecast(entity_id, "hourly")
        if not forecast:
            return None
        closest = min(
            forecast,
            key=lambda f: abs((dt_util.parse_datetime(f["datetime"]) - when).total_seconds()),
        )
        if abs(dt_util.parse_datetime(closest["datetime"]) - when) > FORECAST_MAX_GAP:
            return None  # departure lies beyond the forecast
        temperature = closest.get("temperature")
        return float(temperature) if temperature is not None else None

    @callback
    def _sync_battery_plan(self, departure: datetime | None) -> None:
        """Keep the car's own battery preheat plan on the next departure.

        The battery needs more time than the cabin, so the car does this with
        its plan; we only move the plan's end time to our next departure.
        """
        plan = self._battery_plan()
        if plan is None or not self.settings["battery"] or departure is None:
            return
        end = departure.strftime("%Y%m%d%H%M%S")
        if plan.get("endData") == end and plan.get("isValid") == 1:
            return

        async def sync() -> None:
            try:
                await self.coordinator.client.set_battery_preheat(self.vehicle_id, plan, end)
            except MazdaError as err:
                _LOGGER.warning("Could not move battery preheating to %s: %s", departure, err)
                return
            plan["endData"], plan["isValid"] = end, 1

        self.hass.async_create_background_task(sync(), f"{DOMAIN} battery preheat sync")

    async def async_skip_next(self) -> None:
        departure = self.next_departure()
        if departure is not None:
            await self.async_update(skip=departure.isoformat())

    async def async_unload(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_forecast:
            self._unsub_forecast()
            self._unsub_forecast = None

    # ----------------------------------------------------------- execution

    async def async_start(
        self,
        *,
        source: str,
        departure: datetime | None = None,
        plan: str | None = None,
        **overrides: Any,
    ) -> list[str]:
        """Run the profile now. ``overrides`` replace single profile values.

        Returns the steps that failed; every step is tried even if one fails.
        """
        profile = {**self.settings, **{k: v for k, v in overrides.items() if v is not None}}
        duration = int(profile.get("duration") or profile["lead"])
        duration = max(LEAD_MIN, min(LEAD_MAX, duration))
        departure = departure or dt_util.now() + timedelta(minutes=duration)
        temperature = min(CLIMATE_MAX_TEMP, max(CLIMATE_MIN_TEMP, float(profile["temperature"])))
        client = self.coordinator.client
        vid = self.vehicle_id

        steps: list[tuple[str, Callable[[], Any]]] = [
            ("climate", lambda: client.set_climate(vid, True, temperature, run_time=duration)),
        ]
        if int(profile["seat_heat"]):
            steps.append(("seat_heat", lambda: client.set_seat(vid, "heat", "driver", int(profile["seat_heat"]))))
        if profile["steering_wheel"]:
            steps.append(("steering_wheel", lambda: client.set_steering_wheel_heat(vid, True)))
        if profile["defrost"]:
            steps.append(("defrost", lambda: client.set_defrost(vid, True)))
        battery_plan = self._battery_plan()
        if profile["battery"] and battery_plan is not None:
            end = departure.strftime("%Y%m%d%H%M%S")
            steps.append(("battery", lambda: client.set_battery_preheat(vid, battery_plan, end)))

        failed = await self._async_run(steps)
        self._record("started", source, departure, failed, plan=plan)
        await self.coordinator.async_request_refresh()
        return failed

    async def async_stop(self, *, source: str) -> list[str]:
        """Switch off everything the profile may have switched on."""
        client = self.coordinator.client
        vid = self.vehicle_id
        data = (self.coordinator.data or {}).get(vid)
        status = data.status if data else None
        temperature = (status.target_temperature if status else None) or 21.0
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("climate", lambda: client.set_climate(vid, False, temperature)),
        ]
        if self.settings["seat_heat"] or (status and status.seat_heat_driver):
            steps.append(("seat_heat", lambda: client.set_seat(vid, "heat", "driver", 0)))
        if self.settings["steering_wheel"] or (status and status.steering_wheel_heat_on):
            steps.append(("steering_wheel", lambda: client.set_steering_wheel_heat(vid, False)))
        if self.settings["defrost"] or (status and status.defrost_on):
            steps.append(("defrost", lambda: client.set_defrost(vid, False)))
        failed = await self._async_run(steps)
        self._record("stopped", source, None, failed)
        await self.coordinator.async_request_refresh()
        return failed

    def _battery_plan(self) -> dict[str, Any] | None:
        data = (self.coordinator.data or {}).get(self.vehicle_id)
        return data.battery_preheat_plan if data else None

    async def _async_run(self, steps: list[tuple[str, Callable[[], Any]]]) -> list[str]:
        failed = []
        async with self._running:
            for name, run in steps:
                try:
                    await run()
                except (MazdaError, ValueError) as err:
                    _LOGGER.warning("Pre-conditioning step %s failed: %s", name, err)
                    failed.append(name)
        return failed

    @callback
    def _record(
        self,
        action: str,
        source: str,
        departure: datetime | None,
        failed: list[str],
        *,
        plan: str | None = None,
    ) -> None:
        self.last_run = {
            "action": action,
            "source": source,
            "time": dt_util.now().isoformat(),
            "departure": departure.isoformat() if departure else None,
            "plan": plan,
            "failed": failed,
        }
        self.hass.bus.async_fire(
            EVENT_PRECONDITIONING,
            {"vehicle_id": self.vehicle_id, **self.last_run},
        )
        self._notify()
