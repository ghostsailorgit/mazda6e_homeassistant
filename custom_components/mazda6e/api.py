"""Async client for the backend used by the "MAZDA 6e & CX-6e" app.

This module deliberately has no Home Assistant imports so it can be used
from the command line (see scripts/mazda6e_cli.py) to test an account.

Login flow as done by the app:
  1. POST cma-app-auth/api/login/email-pass-in/v2 with RSA-encrypted e-mail
     and password plus the public half of a client key pair.
  2. If the response says ``emailVerify: true`` the device is unknown. The app
     then asks the backend to e-mail a code (send-email/device-login/send)
     and confirms it (login-device/email-verify).
  3. Every later request carries ``authorization: <token>`` and the same
     ``deviceid``. Expired tokens are renewed with the refresh token.

Remote control as done by the app (climate skips step 1, it needs no PIN):
  1. security-code/get-status shows how many PIN attempts are left,
     security-code/check-code exchanges the 6-digit PIN for an ``rcToken``.
  2. serial-no/get returns a one-time serial number, RSA-encrypted with the
     public key registered at login.
  3. The command endpoint (control/doors, control/air-conditioner, ...) gets
     the command, signed with the matching private key, and answers with a
     ``commandId``.
  4. control/control-result is polled until the car confirms or rejects it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import BASE_URLS
from .crypto import decrypt_serial, encrypt_credential, sign_payload
from .models import Vehicle, VehicleStatus

_LOGGER = logging.getLogger(__name__)

APP_VERSION = "1.2.3"
DEVICE_NAME = "Home Assistant"

HEADERS = {
    "content-type": "application/json",
    "accept": "*/*",
    "appid": "cma",
    "apptype": "IOS",
    "devicetype": "iPhone",
    "appversion": f"V{APP_VERSION}",
    "accept-language": "en-US;q=1.0",
    "language": "en_US",
    "user-agent": f"overseas/{APP_VERSION} (com.mazda.mazda6e; build:1; iOS 18.0.0) Alamofire/5.5.0",
}

# Backend result codes
CODE_TOKEN_EXPIRED = "APP_1_1_02_004"

TIMEOUT = aiohttp.ClientTimeout(total=30)

# control-result codes
RESULT_PENDING = -100
RESULT_SUCCESS = (0, 1201)
RESULT_ALREADY_DONE = 1015

CLIMATE_MIN_TEMP = 16.0
CLIMATE_MAX_TEMP = 30.0
CLIMATE_RUN_TIME = 15  # minutes, like the app's default

CHARGE_LIMIT_MIN = 60
CHARGE_LIMIT_MAX = 100
SEAT_LEVELS = (1, 2, 3)

# flashing-honking "type"
FLASH_ONLY = 1
FLASH_AND_HONK = 3

# serial-no/get "type" per command family, as the app requests it
SERIAL_CONTROL = "1"
SERIAL_CHARGE = "2"
SERIAL_HEATING_PLAN = "5"

COMMAND_TIMEOUT = 90  # seconds the car gets to confirm a command
COMMAND_POLL_INTERVAL = 3

# Sections of condition/v2 and whether the integration asks for them.
STATUS_CRITERIA: dict[str, bool] = {
    "vehicleStatus": True,
    "charge": True,
    "door": True,
    "window": True,
    "hvac": True,
    "tire": True,
    "lamp": True,
    "seat": True,
    "location": False,
    "fuel": False,
    "departurePlan": False,
    "airConditionPlan": False,
    "warmCoolingBox": False,
    "welcome": False,
}


def _check_hhmm(*values: str) -> None:
    for value in values:
        if len(value) != 4 or not value.isdigit() or int(value[:2]) > 23 or int(value[2:]) > 59:
            raise ValueError(f"Time must be HHMM, got {value!r}")


def local_gmt_offset() -> str:
    """Local UTC offset the way the app sends it, e.g. "GMT+02:00"."""
    offset = time.localtime().tm_gmtoff
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset) // 60, 60)
    return f"GMT{sign}{hours:02d}:{minutes:02d}"


class MazdaError(Exception):
    """Base error of this client."""


class MazdaConnectionError(MazdaError):
    """Backend not reachable or answered garbage."""


class MazdaAuthError(MazdaError):
    """Credentials or tokens were rejected; a new login is needed."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class MazdaApiError(MazdaError):
    """Backend rejected a request."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class MazdaPinError(MazdaError):
    """The control PIN is missing, wrong or locked after too many attempts."""

    def __init__(self, message: str, attempts_left: int | None = None) -> None:
        super().__init__(message)
        self.attempts_left = attempts_left


class MazdaCommandError(MazdaError):
    """The car rejected a remote command or did not confirm it in time."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


TokenCallback = Callable[[str, str], Awaitable[None] | None]


class Mazda6eClient:
    """Minimal client for the Mazda 6e cloud."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        region: str,
        device_id: str,
        *,
        token: str | None = None,
        refresh_token: str | None = None,
        on_token_update: TokenCallback | None = None,
        private_key: str | None = None,
        control_pin: str | None = None,
    ) -> None:
        if region not in BASE_URLS:
            raise ValueError(f"Unknown region {region!r}")
        self._session = session
        self._base = BASE_URLS[region]
        self.device_id = device_id
        self.token = token
        self.refresh_token = refresh_token
        self._on_token_update = on_token_update
        self._refresh_lock = asyncio.Lock()
        self.private_key = private_key
        self.control_pin = control_pin
        self.user_id: str | None = None
        # The backend handles one command per car at a time.
        self._command_lock = asyncio.Lock()

    # ------------------------------------------------------------------ http

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = {**HEADERS, "deviceid": self.device_id}
        if auth and self.token:
            headers["authorization"] = self.token
        return headers

    async def _post(self, path: str, body: dict[str, Any], *, auth: bool = True) -> dict[str, Any]:
        url = f"{self._base}/{path}"
        try:
            async with self._session.post(
                url, json=body, headers=self._headers(auth=auth), timeout=TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise MazdaAuthError(f"HTTP {resp.status}")
                if resp.status >= 400:
                    raise MazdaConnectionError(f"HTTP {resp.status} for {path}")
                data = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise MazdaConnectionError(f"Request to {path} failed: {err}") from err
        except ValueError as err:  # invalid JSON
            raise MazdaConnectionError(f"Invalid response from {path}") from err

        if not isinstance(data, dict):
            raise MazdaConnectionError(f"Unexpected response from {path}")
        return data

    async def _request(self, path: str, body: dict[str, Any]) -> Any:
        """Authenticated request with a single token refresh on expiry."""
        data = await self.call(path, body)
        if data.get("success") is True:
            return data.get("data")
        code = data.get("code")
        if code == CODE_TOKEN_EXPIRED:
            raise MazdaAuthError("Token rejected after refresh", code)
        raise MazdaApiError(f"{path}: {code} {data.get('msg')}", code)

    async def call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Authenticated request returning the whole response envelope.

        Refreshes an expired token once; any other backend error is returned,
        not raised, so callers can inspect ``code`` and ``msg``.
        """
        if not self.token:
            raise MazdaAuthError("Not logged in")
        data = await self._post(path, body)
        if data.get("code") == CODE_TOKEN_EXPIRED:
            _LOGGER.debug("Token expired, refreshing")
            await self._refresh(expired_token=self.token)
            data = await self._post(path, body)
        return data

    async def _refresh(self, expired_token: str | None) -> None:
        async with self._refresh_lock:
            if self.token != expired_token:
                return  # someone else refreshed in the meantime
            if not self.refresh_token:
                raise MazdaAuthError("No refresh token")
            data = await self._post(
                "cma-app-auth/api/auth/refresh-token",
                {"refreshToken": self.refresh_token},
            )
            if data.get("success") is not True:
                raise MazdaAuthError("Token refresh failed", data.get("code"))
            await self._store_tokens(data.get("data") or {})

    async def _store_tokens(self, data: dict[str, Any]) -> None:
        token, refresh = data.get("token"), data.get("refreshToken")
        if not token or not refresh:
            raise MazdaAuthError("Backend returned no token")
        self.token, self.refresh_token = token, refresh
        if self._on_token_update:
            result = self._on_token_update(token, refresh)
            if asyncio.iscoroutine(result):
                await result

    # ----------------------------------------------------------------- login

    async def login(self, email: str, password: str, public_key: str) -> bool:
        """Log in. Returns True if the device must be verified via e-mail code."""
        data = await self._post(
            "cma-app-auth/api/login/email-pass-in/v2",
            {
                "loginTime": str(int(time.time())),
                "email": encrypt_credential(email),
                "password": encrypt_credential(password),
                "pubKey": public_key,
            },
            auth=False,
        )
        if data.get("success") is not True:
            raise MazdaAuthError(f"Login rejected: {data.get('msg')}", data.get("code"))
        payload = data.get("data") or {}
        await self._store_tokens(payload)
        if payload.get("userId") is not None:
            self.user_id = str(payload["userId"])
        return bool(payload.get("emailVerify"))

    async def request_device_code(self, email: str) -> None:
        """Ask the backend to e-mail a verification code for this device."""
        await self._request(
            "cma-app-user/api/send-email/device-login/send",
            {
                "email": encrypt_credential(email),
                "deviceName": DEVICE_NAME,
                "loginTime": str(int(time.time())),
                "type": "1",
            },
        )

    async def verify_device_code(self, email: str, code: str) -> None:
        result = await self._request(
            "cma-app-user/api/login-device/email-verify",
            {
                "authCode": code.strip(),
                "email": encrypt_credential(email),
                "deviceName": DEVICE_NAME,
                "deviceModel": DEVICE_NAME,
                "lastLoginTime": str(int(time.time())),
                "type": "3",
            },
        )
        if result is not True:
            raise MazdaApiError("Verification code not accepted")

    # ------------------------------------------------------------------ data

    async def get_vehicles_raw(self) -> list[dict[str, Any]]:
        raw: Any = []
        # The backend has two routes; depending on account one of them is empty.
        for path in ("cma-app-user/api/vehicle/vehicles", "cma-app-user/api/car/vehicles"):
            try:
                raw = await self._request(path, {})
            except MazdaApiError as err:
                _LOGGER.debug("%s not usable: %s", path, err)
                continue
            if raw:
                break
        return [v for v in raw or [] if isinstance(v, dict) and v.get("vin")]

    async def get_vehicles(self) -> list[Vehicle]:
        return [Vehicle.from_api(v) for v in await self.get_vehicles_raw()]

    async def get_status_raw(
        self, vehicle_id: str, criteria: dict[str, bool] | None = None
    ) -> dict[str, Any]:
        """Raw condition. ``criteria`` switches extra sections on or off."""
        sections = {**STATUS_CRITERIA, **(criteria or {})}
        data = await self._request(
            "cma-app-car-condition/api/vehicle/condition/v2",
            {
                "vehicleId": vehicle_id,
                # sic, the typo is part of the API
                "vechileCriteria": {key: "1" if on else "0" for key, on in sections.items()},
            },
        )
        return data if isinstance(data, dict) else {}

    async def get_status(self, vehicle_id: str) -> VehicleStatus:
        return VehicleStatus.from_api(await self.get_status_raw(vehicle_id))

    # --------------------------------------------------------------- control

    async def get_rc_token(self, pin: str | None = None) -> str:
        """Exchange the control PIN for an rcToken.

        Checks the remaining attempts first, like the app, so we never burn the
        last attempt and lock the PIN.
        """
        pin = pin or self.control_pin
        if not pin:
            raise MazdaPinError("No control PIN configured")

        status = await self._request("cma-app-car-control/api/security-code/get-status", {})
        attempts = None
        if isinstance(status, dict) and status.get("retryQuantity") is not None:
            try:
                attempts = int(status["retryQuantity"])
            except (TypeError, ValueError):
                attempts = None
        if attempts is not None and attempts <= 0:
            raise MazdaPinError("PIN locked after too many wrong attempts", 0)

        try:
            data = await self._request(
                "cma-app-car-control/api/security-code/check-code",
                {"safeCode": encrypt_credential(pin)},
            )
        except MazdaApiError as err:
            left = attempts - 1 if attempts is not None else None
            raise MazdaPinError(f"PIN rejected ({err.code})", left) from err
        if not isinstance(data, dict) or not data.get("rcToken"):
            raise MazdaPinError("PIN check returned no rcToken")
        return str(data["rcToken"])

    async def _signed_command(
        self,
        path: str,
        vehicle_id: str,
        params: dict[str, Any],
        *,
        needs_pin: bool,
        pin: str | None = None,
        serial_type: str = "1",
    ) -> dict[str, Any]:
        if not self.private_key:
            raise MazdaAuthError("No control key registered, please log in again")

        async with self._command_lock:
            payload: dict[str, Any] = {**params, "vehicleId": vehicle_id}
            if needs_pin:
                payload["rcToken"] = await self.get_rc_token(pin)

            serial = await self._request(
                "cma-app-car-control/api/serial-no/get", {"type": serial_type}
            )
            if not isinstance(serial, str):
                raise MazdaApiError("Serial number response was empty")
            try:
                payload["seriralNo"] = decrypt_serial(serial, self.private_key)  # sic
            except ValueError as err:
                # Serial was encrypted for another key, e.g. after logging in
                # with the same device elsewhere.
                raise MazdaAuthError("Control key no longer registered") from err
            payload["sign"] = sign_payload(payload, self.private_key)

            data = await self._request(path, payload)
            if not isinstance(data, dict) or not data.get("commandId"):
                raise MazdaApiError("Command was not accepted")
            return await self._wait_for_result(vehicle_id, str(data["commandId"]))

    async def _wait_for_result(self, vehicle_id: str, command_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while True:
            data = await self._request(
                "cma-app-car-control/api/control/control-result",
                {"commandId": command_id, "vehicleId": vehicle_id},
            )
            data = data if isinstance(data, dict) else {}
            code = data.get("resultCode")
            try:
                code = int(code) if code is not None else RESULT_PENDING
            except (TypeError, ValueError):
                code = None
            if code in RESULT_SUCCESS or code == RESULT_ALREADY_DONE:
                return data
            if code != RESULT_PENDING:
                raise MazdaCommandError(
                    f"Car rejected the command: {data.get('errorMsg') or code}", code
                )
            if time.monotonic() >= deadline:
                raise MazdaCommandError("Car did not confirm the command in time")
            await asyncio.sleep(COMMAND_POLL_INTERVAL)

    async def set_locked(self, vehicle_id: str, locked: bool, pin: str | None = None) -> None:
        """Lock (True) or unlock (False) all doors.

        ``pin`` overrides the stored control PIN for this command.
        """
        await self._signed_command(
            "cma-app-car-control/api/control/doors",
            vehicle_id,
            {"command": "lock", "open": not locked},
            needs_pin=True,
            pin=pin,
        )

    async def set_climate(
        self,
        vehicle_id: str,
        enabled: bool,
        temperature: float,
        run_time: int = CLIMATE_RUN_TIME,
    ) -> None:
        """Start (True) or stop (False) remote climate at ``temperature`` °C."""
        if not CLIMATE_MIN_TEMP <= temperature <= CLIMATE_MAX_TEMP:
            raise ValueError(f"Temperature must be {CLIMATE_MIN_TEMP}-{CLIMATE_MAX_TEMP} °C")
        await self._signed_command(
            "cma-app-car-control/api/control/air-conditioner",
            vehicle_id,
            {
                "command": "air",
                "enabled": enabled,
                # tenths of a degree, like the temperatures in the status
                "targetTemp": round(temperature * 10),
                "runTime": run_time,
            },
            needs_pin=False,
        )

    # ------------------------------------------------------ more commands

    async def request_status_update(self, vehicle_id: str) -> None:
        """Wake the car and make it send a fresh status to the cloud."""
        await self._signed_command(
            "cma-app-car-control/api/control/condition-inquiry",
            vehicle_id,
            {"command": "COMMAND_GET_NEW_CONDITION"},
            needs_pin=False,
        )

    async def set_charge_limit(self, vehicle_id: str, percent: int) -> None:
        if not CHARGE_LIMIT_MIN <= percent <= CHARGE_LIMIT_MAX:
            raise ValueError(f"Charge limit must be {CHARGE_LIMIT_MIN}-{CHARGE_LIMIT_MAX} %")
        await self._signed_command(
            "cma-app-car-control/api/charge/percentage",
            vehicle_id,
            {"command": "charge_max", "chargePercentageMax": int(percent)},
            needs_pin=False,
            serial_type=SERIAL_CHARGE,
        )

    async def flash_and_honk(self, vehicle_id: str, action: int = FLASH_AND_HONK) -> None:
        await self._signed_command(
            "cma-app-car-control/api/control/flashing-honking",
            vehicle_id,
            {"command": "flash_bee", "type": action},
            needs_pin=False,
        )

    async def set_seat(
        self, vehicle_id: str, kind: str, seat: str, level: int
    ) -> None:
        """Seat heating (kind "heat") or ventilation ("wind"); level 0 = off.

        seat is "driver" or "passenger". Turning off must send switch 0 without
        a level, the backend rejects level 0.
        """
        if kind not in ("heat", "wind"):
            raise ValueError("kind must be heat or wind")
        prefix = {"driver": "master", "passenger": "copilot"}[seat]
        if level and level not in SEAT_LEVELS:
            raise ValueError("Seat level must be 0-3")
        params: dict[str, Any] = {
            "command": f"seats_{kind}",
            f"{prefix}Switch": 1 if level else 0,
        }
        if level:
            params[f"{prefix}Level"] = level
        await self._signed_command(
            f"cma-app-car-control/api/control/seats/{kind}", vehicle_id, params, needs_pin=False
        )

    async def set_steering_wheel_heat(self, vehicle_id: str, on: bool) -> None:
        await self._signed_command(
            "cma-app-car-control/api/control/steering-wheel/heat",
            vehicle_id,
            {"command": "steering_wheel_heating", "open": on},
            needs_pin=False,
        )

    async def set_defrost(self, vehicle_id: str, on: bool) -> None:
        await self._signed_command(
            "cma-app-car-control/api/control/defrost",
            vehicle_id,
            {"command": "defrost", "enabled": on},
            needs_pin=False,
        )

    async def set_windows(self, vehicle_id: str, open_: bool, pin: str | None = None) -> None:
        """Open or close all windows (needs the control PIN).

        Without ``openType``: with ``openType: 10`` a Mazda 6e (EU) answers
        "The Controller Is Not Responding" while the app's command works.
        """
        await self._signed_command(
            "cma-app-car-control/api/control/windows",
            vehicle_id,
            {"command": "window", "open": open_},
            needs_pin=True,
            pin=pin,
        )

    async def set_trunk(self, vehicle_id: str, open_: bool, pin: str | None = None) -> None:
        """Open or close the tailgate (needs the control PIN)."""
        await self._signed_command(
            "cma-app-car-control/api/control/trunk",
            vehicle_id,
            {"command": "trunk", "open": open_},
            needs_pin=True,
            pin=pin,
        )

    # --------------------------------------------------- plans on the car

    async def get_battery_preheat_plan(self, vehicle_id: str) -> dict[str, Any] | None:
        """The car's battery preheating plan (planType 0), if it has one."""
        plans = await self.get_heating_plans_raw(vehicle_id)
        if not isinstance(plans, list):
            return None
        return next(
            (p for p in plans if isinstance(p, dict) and p.get("planType") == 0), None
        )

    async def set_battery_preheat(
        self, vehicle_id: str, plan: dict[str, Any], departure: str | None
    ) -> None:
        """Enable the preheat plan for ``departure`` (YYYYmmddHHMMSS) or disable it (None)."""
        if departure is None:
            path = "cma-app-car-control/api/heating-plans/plan-availability"
            params = {
                "command": "COMMAND_HEATING_PLANS_AVAILABILITY",
                "enabled": False,
                "planId": str(plan["planId"]),
            }
        else:
            path = "cma-app-car-control/api/heating-plans/update-plan"
            params = {
                "command": "COMMAND_HEATING_PLANS_UPDATE",
                "endData": departure,
                "planId": str(plan["planId"]),
                "planType": plan.get("planType", 0),
            }
        await self._signed_command(
            path, vehicle_id, params, needs_pin=False, serial_type=SERIAL_HEATING_PLAN
        )

    async def set_charge_plan(
        self,
        vehicle_id: str,
        plan: dict[str, Any],
        *,
        start: str,
        end: str,
    ) -> dict[str, Any]:
        """Change the times of the car's charging schedule. start/end as "HHMM".

        The car reports plans without a time zone, so the local one is sent
        unless the plan carries its own. Switching the plan on or off is
        ``set_charge_plan_enabled``.
        """
        _check_hhmm(start, end)
        if plan.get("planId") is None:
            raise ValueError("The car reported a charging plan without id")
        return await self._signed_command(
            "cma-app-car-control/api/charge/modify-plan",
            vehicle_id,
            {
                "command": "modify-plan",
                "planId": str(plan["planId"]),
                "planType": plan.get("planType", 1),
                "startTime": start,
                "endTime": end,
                "endSwitch": plan.get("endSwitch", 1),
                "timeFormat": plan.get("timeFormat", 1),
                "timeZone": plan.get("timeZone") or local_gmt_offset(),
            },
            needs_pin=False,
            serial_type=SERIAL_CHARGE,
        )

    async def add_charge_plan(
        self, vehicle_id: str, *, start: str, end: str, end_enabled: bool = True
    ) -> dict[str, Any]:
        """Create a charging schedule (start/end as "HHMM") in the local time zone."""
        _check_hhmm(start, end)
        return await self._signed_command(
            "cma-app-car-control/api/charge/add-plan",
            vehicle_id,
            {
                "command": "add_charge_plan",
                "planType": 1,
                "startTime": start,
                "endTime": end,
                "endSwitch": 1 if end_enabled else 0,
                "timeFormat": 1,
                "timeZone": local_gmt_offset(),
            },
            needs_pin=False,
            serial_type=SERIAL_CHARGE,
        )

    async def delete_charge_plan(self, vehicle_id: str, plan_id: str) -> dict[str, Any]:
        return await self._signed_command(
            "cma-app-car-control/api/charge/delete-plan",
            vehicle_id,
            {"command": "delete_charge_plan", "planId": str(plan_id)},
            needs_pin=False,
            serial_type=SERIAL_CHARGE,
        )

    async def set_charge_plan_enabled(
        self, vehicle_id: str, plan_id: str, enabled: bool
    ) -> dict[str, Any]:
        return await self._signed_command(
            "cma-app-car-control/api/charge/validity",
            vehicle_id,
            {"command": "COMMAND_VALID_CHARGE_PLAN", "planId": str(plan_id), "enabled": enabled},
            needs_pin=False,
            serial_type=SERIAL_CHARGE,
        )

    async def get_heating_plans_raw(self, vehicle_id: str) -> Any:
        return await self._request(
            "cma-app-car-control/api/heating-plans/query/list", {"vehicleId": vehicle_id}
        )

    async def get_functions(self, vehicle_id: str) -> set[str]:
        """Function codes the car supports, e.g. "#driverSeatHeat"; empty if unknown."""
        data = await self._request(
            "cma-app-user/api/vehicle/function-config", {"vehicleId": vehicle_id}
        )
        codes = (data or {}).get("confList") if isinstance(data, dict) else None
        return {str(c) for c in codes} if isinstance(codes, list) else set()
