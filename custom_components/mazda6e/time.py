"""Times: departure per weekday, the car's charging and battery preheat plans."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import Mazda6eConfigEntry
from .entity import Mazda6eControlEntity, Mazda6ePlanEntity, has_control, plan_entities
from .precondition import WEEKDAYS, parse_time
from .switch import charge_plan_enabled

BATTERY_PREHEAT_TIME = TimeEntityDescription(
    key="battery_preheat_time", translation_key="battery_preheat_time", icon="mdi:clock-outline"
)
CHARGE_START = TimeEntityDescription(
    key="charge_schedule_start", translation_key="charge_schedule_start", icon="mdi:clock-start"
)
CHARGE_END = TimeEntityDescription(
    key="charge_schedule_end", translation_key="charge_schedule_end", icon="mdi:clock-end"
)

PREHEAT_FORMAT = "%Y%m%d%H%M%S"


def _departure(day: str) -> TimeEntityDescription:
    return TimeEntityDescription(
        key=f"precondition_departure_{day}",
        translation_key=f"precondition_departure_{day}",
        icon="mdi:car-clock",
        entity_category=EntityCategory.CONFIG,
    )


def _hhmm(value: object) -> time | None:
    text = str(value or "")
    if len(text) != 4 or not text.isdigit():
        return None
    return time(int(text[:2]), int(text[2:]))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Mazda6eConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not has_control(entry):
        return
    coordinator = entry.runtime_data
    entities: list[TimeEntity] = []
    for vid, data in coordinator.data.items():
        if data.battery_preheat_plan is not None:
            entities.append(Mazda6eBatteryPreheatTime(coordinator, vid, BATTERY_PREHEAT_TIME))
        if data.status.charge_plan is not None:
            entities.append(Mazda6eChargeTime(coordinator, vid, CHARGE_START, "startTime"))
            entities.append(Mazda6eChargeTime(coordinator, vid, CHARGE_END, "endTime"))
    async_add_entities(entities)
    async_add_entities(
        plan_entities(
            entry,
            lambda vid, p: [Mazda6eDepartureTime(coordinator, vid, _departure(day), p, day) for day in WEEKDAYS],
        )
    )


class Mazda6eBatteryPreheatTime(Mazda6eControlEntity, TimeEntity):
    """Time the car's battery should be warm (its own preheat plan)."""

    @property
    def native_value(self) -> time | None:
        plan = self.data.battery_preheat_plan if self.data else None
        try:
            # Wall-clock time of the car, no time zone involved.
            return datetime.strptime(plan["endData"], PREHEAT_FORMAT).time()  # noqa: DTZ007
        except (KeyError, TypeError, ValueError):
            return None

    async def async_set_value(self, value: time) -> None:
        plan = self.data.battery_preheat_plan
        # Next occurrence of that time; the plan takes a full timestamp.
        now = dt_util.now()
        end_dt = now.replace(hour=value.hour, minute=value.minute, second=0, microsecond=0)
        if end_dt <= now:
            end_dt += timedelta(days=1)
        end = end_dt.strftime(PREHEAT_FORMAT)
        await self._async_command(self.coordinator.client.set_battery_preheat(self._vehicle_id, plan, end), {})
        plan["endData"], plan["isValid"] = end, 1
        self.async_write_ha_state()


class Mazda6eChargeTime(Mazda6eControlEntity, TimeEntity):
    def __init__(self, coordinator, vehicle_id, description, plan_key: str) -> None:
        super().__init__(coordinator, vehicle_id, description)
        self._plan_key = plan_key

    @property
    def native_value(self) -> time | None:
        plan = self.status.charge_plan
        return _hhmm(plan.get(self._plan_key)) if plan else None

    async def async_set_value(self, value: time) -> None:
        plan = self.status.charge_plan
        times = {
            "startTime": str(plan.get("startTime") or "0000"),
            "endTime": str(plan.get("endTime") or "0000"),
        }
        times[self._plan_key] = value.strftime("%H%M")
        await self._async_command(
            self.coordinator.client.set_charge_plan(
                self._vehicle_id,
                plan,
                start=times["startTime"],
                end=times["endTime"],
                enabled=charge_plan_enabled(plan),
            ),
            {},
        )
        plan.update(times)
        self.async_write_ha_state()


class Mazda6eDepartureTime(Mazda6ePlanEntity, TimeEntity):
    def __init__(self, coordinator, vehicle_id, description, preconditioner, day: str) -> None:
        super().__init__(coordinator, vehicle_id, description, preconditioner)
        self._day = day

    @property
    def native_value(self) -> time:
        return parse_time(self.preconditioner.settings["days"][self._day]["time"])

    async def async_set_value(self, value: time) -> None:
        await self.preconditioner.async_update(day=self._day, time=value.strftime("%H:%M"))
