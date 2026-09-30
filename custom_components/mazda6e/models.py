"""Typed views of the vehicle data returned by the Mazda 6e backend.

The backend answers with fairly raw CAN-style values (tenths of degrees,
sentinel values, integer enums). Everything is normalised here so the Home
Assistant entities only deal with clean Python values. The module has no
Home Assistant dependency so it can be unit tested and used from the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# remainChargeTime reports 0x1FFF when the car has no estimate.
CHARGE_TIME_UNKNOWN = 8191

# chargeStatus values observed in the app.
CHARGE_STATUS = {
    0: "not_charging",
    4: "completed",
    6: "charging",
    7: "paused",
}

VEHICLE_STATE = {1: "driving", 2: "parked"}
POWER_STATE = {0: "off", 1: "accessory", 2: "on"}

# Position order of the "doors" / "windows" arrays in the condition response.
POSITIONS = ("front_left", "front_right", "rear_left", "rear_right")


@dataclass
class Vehicle:
    """A vehicle registered on the account."""

    vehicle_id: str
    vin: str
    model_name: str | None = None
    series_name: str | None = None
    nickname: str | None = None
    plate_number: str | None = None

    @property
    def display_name(self) -> str:
        return self.nickname or self.model_name or self.series_name or "Mazda 6e"

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Vehicle:
        return cls(
            vehicle_id=str(raw.get("carId") or raw["vehicleId"]),
            vin=raw["vin"],
            model_name=raw.get("modelName"),
            series_name=raw.get("seriesName"),
            nickname=raw.get("carName"),
            plate_number=raw.get("plateNumber"),
        )


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _flag(value: Any) -> bool | None:
    """Interpret 0/1 style flags; None stays unknown."""
    number = _num(value)
    if number is None:
        return None
    return number != 0


def _tenths(value: Any) -> float | None:
    """Temperatures come in 1/10 °C; 0 means 'not reported'."""
    number = _num(value)
    if not number:
        return None
    return round(number / 10, 1)


def _index(values: Any, idx: int) -> Any:
    if isinstance(values, list) and idx < len(values):
        return values[idx]
    return None


@dataclass
class VehicleStatus:
    """Normalised snapshot of /vehicle/condition/v2."""

    # battery / range
    soc: int | None = None
    range_km: int | None = None
    odometer_km: float | None = None

    # charging
    charge_status: str | None = None
    charge_status_code: int | None = None
    plugged_in: bool | None = None
    dc_plugged_in: bool | None = None
    charge_current: float | None = None
    ac_charge_current: float | None = None
    dc_charge_current: float | None = None
    remaining_charge_minutes: int | None = None
    charge_limit: int | None = None

    # locks, doors, windows
    driver_locked: bool | None = None
    passenger_locked: bool | None = None
    doors_open: dict[str, bool | None] = field(default_factory=dict)
    windows_open: dict[str, bool | None] = field(default_factory=dict)
    trunk_open: bool | None = None
    hood_open: bool | None = None

    # climate
    inside_temperature: float | None = None
    target_temperature: float | None = None
    climate_on: bool | None = None
    defrost_on: bool | None = None
    steering_wheel_heat_on: bool | None = None
    inside_humidity: float | None = None
    inside_pm25: float | None = None

    # seats: level 0 (off) to 3
    seat_heat_driver: int | None = None
    seat_heat_passenger: int | None = None
    seat_heat_rear_left: int | None = None
    seat_heat_rear_right: int | None = None
    seat_vent_driver: int | None = None
    seat_vent_passenger: int | None = None

    # exterior lights
    lamps: dict[str, bool | None] = field(default_factory=dict)

    # first charging schedule the car reports (raw, needed to modify it)
    charge_plan: dict[str, Any] | None = None

    # general
    vehicle_state: str | None = None
    power_state: str | None = None
    speed_kmh: float | None = None
    online: bool | None = None
    latitude: float | None = None
    longitude: float | None = None
    tire_pressure_bar: dict[str, float | None] = field(default_factory=dict)
    last_update: datetime | None = None

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_charging(self) -> bool | None:
        if self.charge_status_code is None:
            return None
        return self.charge_status == "charging"

    @property
    def locked(self) -> bool | None:
        """True if the car is locked (every known lock reports locked)."""
        known = [v for v in (self.driver_locked, self.passenger_locked) if v is not None]
        if not known:
            return None
        return all(known)

    @property
    def any_door_open(self) -> bool | None:
        values = [*self.doors_open.values(), self.trunk_open, self.hood_open]
        known = [v for v in values if v is not None]
        if not known:
            return None
        return any(known)

    @property
    def any_window_open(self) -> bool | None:
        known = [v for v in self.windows_open.values() if v is not None]
        if not known:
            return None
        return any(known)

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> VehicleStatus:
        raw = raw or {}
        status = raw.get("vehicleStatus") or {}
        charge = raw.get("charge") or {}
        door = raw.get("door") or {}
        window = raw.get("window") or {}
        hvac = raw.get("hvac") or {}
        tire = raw.get("tire") or {}
        location = raw.get("location") or {}
        seat = raw.get("seat") or {}
        lamp = raw.get("lamp") or {}
        plans = charge.get("chargePlanList")
        charge_plan = plans[0] if isinstance(plans, list) and plans and isinstance(plans[0], dict) else None

        def seat_level(position: str, key: str) -> int | None:
            data = seat.get(position)
            if not isinstance(data, dict):
                return None
            value = data.get(key)
            if value is None and key == "heatStatus":
                value = data.get("level")
            level = _int(value)
            return max(level, 0) if level is not None else None

        charge_code = _int(charge.get("chargeStatus"))
        remaining = _int(charge.get("remainChargeTime"))
        if remaining is not None and (remaining >= CHARGE_TIME_UNKNOWN or remaining < 0):
            remaining = None

        plugged = _flag(charge.get("chargeConStatus"))
        # An active charge implies a connected cable even if the flag lags.
        if charge_code in (4, 6, 7):
            plugged = True

        # driverLock/passengerLock: 0 = locked, 1 = unlocked
        driver_lock = _int(door.get("driverLock"))
        passenger_lock = _int(door.get("passengerLock"))

        speed = _num(status.get("speed"))
        if speed is not None and not 0 <= speed <= 250:
            speed = None

        last_update = None
        ts = _int(raw.get("lastUpdatedAt"))
        if ts and ts > 0:
            last_update = datetime.fromtimestamp(ts / 1000, tz=UTC)

        tire_keys = {
            "front_left": "leftFront",
            "front_right": "rightFront",
            "rear_left": "leftBack",
            "rear_right": "rightBack",
        }
        tire_pressure = {}
        for pos, key in tire_keys.items():
            kpa = _num((tire.get(key) or {}).get("pressure"))
            tire_pressure[pos] = round(kpa / 100, 2) if kpa else None

        return cls(
            soc=_int(status.get("soc")),
            range_km=_int(status.get("drvMileage")),
            odometer_km=_num(status.get("totalMileage")),
            charge_status=(
                CHARGE_STATUS.get(charge_code, "unknown") if charge_code is not None else None
            ),
            charge_status_code=charge_code,
            plugged_in=plugged,
            dc_plugged_in=_flag(charge.get("dcChargeGunConnectStatus")),
            charge_current=_num(charge.get("chargeCurrent")),
            ac_charge_current=_num(charge.get("acChargeCurrent")),
            dc_charge_current=_num(charge.get("dcChargeCurrent")),
            remaining_charge_minutes=remaining,
            charge_limit=_int(charge.get("maxSocPercent")),
            driver_locked=None if driver_lock is None else driver_lock == 0,
            passenger_locked=None if passenger_lock is None else passenger_lock == 0,
            doors_open={
                pos: _flag(_index(door.get("doors"), i)) for i, pos in enumerate(POSITIONS)
            },
            windows_open={
                pos: _flag(_index(window.get("windows"), i)) for i, pos in enumerate(POSITIONS)
            },
            trunk_open=_flag(door.get("trunk")),
            hood_open=_flag(door.get("hood")),
            inside_temperature=_tenths(hvac.get("insideTemp")),
            target_temperature=_tenths(hvac.get("remoteTemp")),
            climate_on=_flag(hvac.get("acStatus")),
            defrost_on=_flag(hvac.get("defrostStatus")),
            steering_wheel_heat_on=_flag(status.get("steeringWheelHeater")),
            inside_humidity=_num(hvac.get("insideHumidity")),
            inside_pm25=_num(hvac.get("insidePm25")),
            seat_heat_driver=seat_level("leftFront", "heatStatus"),
            seat_heat_passenger=seat_level("rightFront", "heatStatus"),
            seat_heat_rear_left=seat_level("leftBack", "heatStatus"),
            seat_heat_rear_right=seat_level("rightBack", "heatStatus"),
            seat_vent_driver=seat_level("leftFront", "ventStatus"),
            seat_vent_passenger=seat_level("rightFront", "ventStatus"),
            lamps={
                name: _flag(lamp.get(key))
                for name, key in (
                    ("low_beam", "lowBeam"),
                    ("high_beam", "highBeam"),
                    ("position", "positionLamp"),
                    ("left_turn", "leftTurn"),
                    ("right_turn", "rightTurn"),
                )
            },
            charge_plan=charge_plan,
            vehicle_state=VEHICLE_STATE.get(_int(status.get("status"))),
            power_state=POWER_STATE.get(_int(status.get("powerStatus"))),
            speed_kmh=speed,
            online=_flag(status.get("connectStatus")),
            latitude=_first_num(location, "latitude", "lat", "gpsLatitude"),
            longitude=_first_num(location, "longitude", "lng", "lon", "gpsLongitude"),
            tire_pressure_bar=tire_pressure,
            last_update=last_update,
            raw=raw,
        )


def _first_num(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _num(data.get(key))
        if value is not None:
            return value
    return None
