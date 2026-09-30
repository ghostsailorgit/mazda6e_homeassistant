from mazda6e.models import Vehicle, VehicleStatus

SAMPLE = {
    "vehicleStatus": {
        "soc": 78,
        "drvMileage": 390,
        "totalMileage": 12345,
        "status": 2,
        "powerStatus": 0,
        "speed": 0,
        "connectStatus": 1,
    },
    "charge": {
        "chargeStatus": 6,
        "chargeConStatus": 1,
        "chargeCurrent": 15.5,
        "acChargeCurrent": 15.5,
        "dcChargeCurrent": 0,
        "remainChargeTime": 95,
        "maxSocPercent": 90,
        "dcChargeGunConnectStatus": 0,
    },
    "door": {
        "doors": [0, 1, 0, 0],
        "trunk": 0,
        "hood": 0,
        "driverLock": 1,
        "passengerLock": 0,
    },
    "window": {"windows": [0, 0, 0, 1], "openDegree": [0, 0, 0, 30]},
    "hvac": {"insideTemp": 215, "remoteTemp": 0, "acStatus": 0},
    "tire": {"leftFront": {"pressure": 250}},
    "location": {"latitude": "49.01", "longitude": "8.40"},
    "lastUpdatedAt": 1759200000000,
}


def test_parse_full_status():
    s = VehicleStatus.from_api(SAMPLE)
    assert s.soc == 78
    assert s.range_km == 390
    assert s.charge_status == "charging"
    assert s.is_charging is True
    assert s.plugged_in is True
    assert s.charge_current == 15.5
    assert s.remaining_charge_minutes == 95
    assert s.charge_limit == 90
    assert s.driver_locked is False
    assert s.passenger_locked is True
    assert s.locked is False  # one lock open -> car counts as unlocked
    assert s.doors_open == {
        "front_left": False,
        "front_right": True,
        "rear_left": False,
        "rear_right": False,
    }
    assert s.any_door_open is True
    assert s.any_window_open is True
    assert s.inside_temperature == 21.5
    assert s.target_temperature is None  # 0 means "not reported"
    assert s.vehicle_state == "parked"
    assert s.online is True
    assert s.tire_pressure_bar["front_left"] == 2.5
    assert s.tire_pressure_bar["rear_right"] is None
    assert (s.latitude, s.longitude) == (49.01, 8.40)
    assert s.last_update.year == 2025


def test_all_locked_and_closed():
    raw = {"door": {"doors": [0, 0, 0, 0], "trunk": 0, "hood": 0, "driverLock": 0, "passengerLock": 0}}
    s = VehicleStatus.from_api(raw)
    assert s.locked is True
    assert s.any_door_open is False


def test_empty_response_is_all_unknown():
    s = VehicleStatus.from_api(None)
    assert s.soc is None
    assert s.locked is None
    assert s.any_door_open is None
    assert s.is_charging is None
    assert s.doors_open["front_left"] is None


def test_charge_time_sentinel_and_plug_inference():
    s = VehicleStatus.from_api({"charge": {"remainChargeTime": 8191, "chargeStatus": 4, "chargeConStatus": 0}})
    assert s.remaining_charge_minutes is None
    assert s.charge_status == "completed"
    assert s.plugged_in is True
    assert s.is_charging is False


def test_unknown_charge_status():
    assert VehicleStatus.from_api({"charge": {"chargeStatus": 3}}).charge_status == "unknown"


def test_implausible_speed_dropped():
    assert VehicleStatus.from_api({"vehicleStatus": {"speed": 511}}).speed_kmh is None


def test_vehicle_from_api():
    v = Vehicle.from_api({"carId": 123, "vin": "JMZ123", "modelName": "MAZDA 6e", "carName": ""})
    assert v.vehicle_id == "123"
    assert v.display_name == "MAZDA 6e"
