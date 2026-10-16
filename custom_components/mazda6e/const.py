"""Constants for the Mazda 6e integration."""

from __future__ import annotations

DOMAIN = "mazda6e"

CONF_REGION = "region"
CONF_DEVICE_ID = "device_id"
CONF_TOKEN = "token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CONTROL_PRIVATE_KEY = "control_private_key"
CONF_CONTROL_PIN = "control_pin"
CONF_STORE_PIN = "store_pin"

REGION_EUROPE = "europe"
REGION_ASIA = "asia"

# Config subentry type of a departure plan and the keys of its data.
SUBENTRY_PLAN = "departure_plan"
PLAN_VEHICLE = "vehicle_id"
PLAN_TIME = "time"
PLAN_WEEKDAYS = "weekdays"
PLAN_TEMPERATURE = "temperature"
PLAN_ENABLED = "enabled"
# Frost protection: defrost and steering wheel heating on top of the profile
# when the forecast at departure is at or below PLAN_FROST_BELOW (°C).
PLAN_FROST = "frost"
PLAN_FROST_BELOW = "frost_below"
FROST_BELOW_DEFAULT = 3.0
FROST_BELOW_MIN, FROST_BELOW_MAX = -20.0, 15.0

# Backends used by the official "MAZDA 6e & CX-6e" app. The car is built on a
# Changan platform, so the app talks to Changan's "CMA" gateway, not to the
# classic MyMazda backend.
BASE_URLS = {
    REGION_EUROPE: "https://cma-m.iov.changanauto.com.de/cma-app-gw",
    REGION_ASIA: "https://cma.iov.changanauto.sg/cma-app-gw",
}

DEFAULT_SCAN_INTERVAL = 5  # minutes
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 60
