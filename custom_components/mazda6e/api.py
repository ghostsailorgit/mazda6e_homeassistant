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

Remote control (lock/unlock) as done by the app:
  1. security-code/get-status shows how many PIN attempts are left,
     security-code/check-code exchanges the 6-digit PIN for an ``rcToken``.
  2. serial-no/get returns a one-time serial number, RSA-encrypted with the
     public key registered at login.
  3. control/doors gets the command, signed with the matching private key,
     and answers with a ``commandId``.
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

COMMAND_TIMEOUT = 90  # seconds the car gets to confirm a command
COMMAND_POLL_INTERVAL = 3


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
        if not self.token:
            raise MazdaAuthError("Not logged in")

        for attempt in range(2):
            data = await self._post(path, body)
            if data.get("success") is True:
                return data.get("data")

            code = data.get("code")
            if code == CODE_TOKEN_EXPIRED and attempt == 0:
                _LOGGER.debug("Token expired, refreshing")
                await self._refresh(expired_token=self.token)
                continue
            if code == CODE_TOKEN_EXPIRED:
                raise MazdaAuthError("Token rejected after refresh", code)
            raise MazdaApiError(f"{path}: {code} {data.get('msg')}", code)

        raise MazdaAuthError("Token refresh loop")  # pragma: no cover

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

    async def get_vehicles(self) -> list[Vehicle]:
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
        return [Vehicle.from_api(v) for v in raw or [] if isinstance(v, dict) and v.get("vin")]

    async def get_status_raw(self, vehicle_id: str) -> dict[str, Any]:
        data = await self._request(
            "cma-app-car-condition/api/vehicle/condition/v2",
            {
                "vehicleId": vehicle_id,
                "vechileCriteria": {  # sic, typo is part of the API
                    "vehicleStatus": "1",
                    "charge": "1",
                    "door": "1",
                    "window": "1",
                    "hvac": "1",
                    "tire": "1",
                    "lamp": "1",
                    "seat": "1",
                    "location": "1",
                    "fuel": "0",
                    "departurePlan": "0",
                    "airConditionPlan": "0",
                    "warmCoolingBox": "0",
                    "welcome": "0",
                },
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
    ) -> dict[str, Any]:
        if not self.private_key:
            raise MazdaAuthError("No control key registered, please log in again")

        async with self._command_lock:
            payload: dict[str, Any] = {**params, "vehicleId": vehicle_id}
            if needs_pin:
                payload["rcToken"] = await self.get_rc_token(pin)

            serial = await self._request("cma-app-car-control/api/serial-no/get", {"type": "1"})
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
