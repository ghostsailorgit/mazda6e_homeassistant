"""Client tests against a local fake of the Mazda backend."""

import asyncio
import base64

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from mazda6e import api as api_mod
from mazda6e import const
from mazda6e.crypto import encrypt_credential, generate_key_pair


def test_encrypt_credential_roundtrip():
    public, private = generate_key_pair()
    encrypted = encrypt_credential("secret@example.com", public)
    key = serialization.load_der_private_key(base64.b64decode(private), None)
    plain = key.decrypt(base64.b64decode(encrypted), padding.PKCS1v15())
    assert plain == b"secret@example.com"


def test_encrypt_with_server_key():
    # Must not raise and must produce a 256 byte ciphertext.
    assert len(base64.b64decode(encrypt_credential("x"))) == 256


class FakeBackend:
    def __init__(self):
        self.valid_token = "t1"
        self.calls: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/{path:.*}", self.handle)
        return app

    async def handle(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        self.calls.append(path)
        body = await request.json()
        assert request.headers["deviceid"] == "dev-1"

        if path.endswith("login/email-pass-in/v2"):
            assert body["email"] and body["password"] and body["pubKey"]
            return web.json_response(
                {"success": True, "data": {"token": "t1", "refreshToken": "r1", "emailVerify": True}}
            )
        if path.endswith("auth/refresh-token"):
            if body["refreshToken"] != "r1":
                return web.json_response({"success": False, "code": "X"})
            self.valid_token = "t2"
            return web.json_response({"success": True, "data": {"token": "t2", "refreshToken": "r2"}})

        if request.headers.get("authorization") != self.valid_token:
            return web.json_response({"success": False, "code": const_token_expired()})

        if path.endswith("device-login/send"):
            return web.json_response({"success": True, "data": True})
        if path.endswith("email-verify"):
            return web.json_response({"success": True, "data": body["authCode"] == "123456"})
        if path.endswith("vehicle/vehicles"):
            return web.json_response({"success": True, "data": []})
        if path.endswith("car/vehicles"):
            return web.json_response(
                {"success": True, "data": [{"carId": 42, "vin": "VIN", "modelName": "MAZDA 6e"}]}
            )
        if path.endswith("condition/v2"):
            assert body["vehicleId"] == "42"
            return web.json_response({"success": True, "data": {"vehicleStatus": {"soc": 55}}})
        return web.json_response({"success": False, "code": "404"})


def const_token_expired() -> str:
    return api_mod.CODE_TOKEN_EXPIRED


async def _with_client(backend: FakeBackend, fn, **kwargs):
    server = TestServer(backend.app())
    await server.start_server()
    try:
        const.BASE_URLS["test"] = str(server.make_url("")).rstrip("/")
        async with aiohttp.ClientSession() as session:
            client = api_mod.Mazda6eClient(session, "test", "dev-1", **kwargs)
            return await fn(client)
    finally:
        const.BASE_URLS.pop("test", None)
        await server.close()


def test_login_verify_and_status():
    backend = FakeBackend()

    async def run(client):
        needs_code = await client.login("a@b.c", "pw", "PUBKEY")
        assert needs_code is True
        await client.request_device_code("a@b.c")
        with pytest.raises(api_mod.MazdaApiError):
            await client.verify_device_code("a@b.c", "000000")
        await client.verify_device_code("a@b.c", " 123456 ")
        vehicles = await client.get_vehicles()
        assert [v.vehicle_id for v in vehicles] == ["42"]
        status = await client.get_status("42")
        assert status.soc == 55

    asyncio.run(_with_client(backend, run))


def test_token_refresh_is_persisted():
    backend = FakeBackend()
    backend.valid_token = "t2"  # our stored token "t1" is expired
    stored = []

    async def run(client):
        vehicles = await client.get_vehicles()
        assert vehicles[0].vin == "VIN"
        assert client.token == "t2"

    asyncio.run(
        _with_client(
            backend,
            run,
            token="t1",
            refresh_token="r1",
            on_token_update=lambda t, r: stored.append((t, r)),
        )
    )
    assert stored == [("t2", "r2")]
    assert backend.calls.count("cma-app-auth/api/auth/refresh-token") == 1


def test_failed_refresh_raises_auth_error():
    backend = FakeBackend()
    backend.valid_token = "other"

    async def run(client):
        with pytest.raises(api_mod.MazdaAuthError):
            await client.get_status("42")

    asyncio.run(_with_client(backend, run, token="t1", refresh_token="bad"))


def test_unreachable_backend():
    async def run():
        const.BASE_URLS["test"] = "http://127.0.0.1:1"
        try:
            async with aiohttp.ClientSession() as session:
                client = api_mod.Mazda6eClient(session, "test", "dev-1", token="t")
                with pytest.raises(api_mod.MazdaConnectionError):
                    await client.get_status("1")
        finally:
            const.BASE_URLS.pop("test", None)

    asyncio.run(run())


# --------------------------------------------------------------- remote lock

class FakeControlBackend(FakeBackend):
    """Adds the remote-control endpoints and really checks the signature."""

    PIN = "123456"

    def __init__(self, public_key: str, results: list[int | None]):
        super().__init__()
        self.public_key = serialization.load_der_public_key(base64.b64decode(public_key))
        self.results = list(results)
        self.attempts = 5
        self.serial = "SERIAL-777"
        self.commands: list[dict] = []
        self.command_paths: list[str] = []
        self.serial_types: list[str] = []
        self.heating_plans: list[dict] = [{"planId": 7, "planType": 0, "isValid": 1, "endData": "20261005073000"}]

    async def handle(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        if "cma-app-car-control" not in path:
            return await super().handle(request)
        self.calls.append(path)
        body = await request.json()
        assert request.headers.get("authorization") == self.valid_token

        if path.endswith("security-code/get-status"):
            return web.json_response({"success": True, "data": {"retryQuantity": self.attempts}})
        if path.endswith("security-code/check-code"):
            server_key = serialization.load_der_private_key(base64.b64decode(SERVER_PRIVATE), None)
            pin = server_key.decrypt(base64.b64decode(body["safeCode"]), padding.PKCS1v15()).decode()
            if pin != self.PIN:
                self.attempts -= 1
                return web.json_response({"success": False, "code": "PIN_WRONG"})
            return web.json_response({"success": True, "data": {"rcToken": "rc-1"}})
        if path.endswith("heating-plans/query/list"):
            assert body == {"vehicleId": "42"}
            return web.json_response({"success": True, "data": self.heating_plans})
        if path.endswith("serial-no/get"):
            self.serial_types.append(body["type"])
            enc = self.public_key.encrypt(self.serial.encode(), padding.PKCS1v15())
            return web.json_response({"success": True, "data": base64.encodebytes(enc).decode()})
        if path.endswith("control/control-result"):
            assert body == {"commandId": "cmd-1", "vehicleId": "42"}
            code = self.results.pop(0)
            return web.json_response({"success": True, "data": {"resultCode": code, "errorMsg": "nope"}})
        if "sign" in body:  # any signed command
            canonical = "&".join(
                f"{k}={str(v).lower() if isinstance(v, bool) else v}"
                for k, v in sorted(body.items())
                if k not in ("sign", "command")
            )
            self.public_key.verify(  # raises InvalidSignature -> HTTP 500
                base64.b64decode(body["sign"]), canonical.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            assert body["seriralNo"] == self.serial
            if path.endswith("control/doors"):
                assert body["rcToken"] == "rc-1"
            self.commands.append(body)
            self.command_paths.append(path.removeprefix("cma-app-car-control/api/"))
            return web.json_response({"success": True, "data": {"commandId": "cmd-1"}})
        return web.json_response({"success": False, "code": "404"})


# Test double for the key pair baked into the app: encrypt_credential is
# pointed at SERVER_PUBLIC so the fake server can decrypt the PIN.
SERVER_PUBLIC, SERVER_PRIVATE = generate_key_pair()


@pytest.fixture
def control(monkeypatch):
    monkeypatch.setattr(api_mod, "COMMAND_POLL_INTERVAL", 0)
    monkeypatch.setattr(
        api_mod, "encrypt_credential", lambda value: encrypt_credential(value, SERVER_PUBLIC)
    )
    public, private = generate_key_pair()
    return public, private


def _run_control(backend, private, fn, pin=FakeControlBackend.PIN):
    return asyncio.run(
        _with_client(backend, fn, token="t1", refresh_token="r1", private_key=private, control_pin=pin)
    )


def test_lock_signed_and_confirmed(control):
    public, private = control
    backend = FakeControlBackend(public, [-100, None, 0])

    async def run(client):
        await client.set_locked("42", True)

    _run_control(backend, private, run)
    assert backend.commands[0]["open"] is False
    assert backend.commands[0]["command"] == "lock"
    assert backend.calls.count("cma-app-car-control/api/control/control-result") == 3


def test_unlock_already_done_is_success(control):
    public, private = control
    backend = FakeControlBackend(public, [1015])

    async def run(client):
        await client.set_locked("42", False)

    _run_control(backend, private, run)
    assert backend.commands[0]["open"] is True


def test_rejected_command(control):
    public, private = control
    backend = FakeControlBackend(public, [-100, -1])

    async def run(client):
        with pytest.raises(api_mod.MazdaCommandError) as err:
            await client.set_locked("42", True)
        assert err.value.code == -1

    _run_control(backend, private, run)


def test_command_timeout(control, monkeypatch):
    public, private = control
    monkeypatch.setattr(api_mod, "COMMAND_TIMEOUT", 0)
    backend = FakeControlBackend(public, [-100])

    async def run(client):
        with pytest.raises(api_mod.MazdaCommandError):
            await client.set_locked("42", True)

    _run_control(backend, private, run)


def test_wrong_pin_reports_attempts(control):
    public, private = control
    backend = FakeControlBackend(public, [])

    async def run(client):
        with pytest.raises(api_mod.MazdaPinError) as err:
            await client.set_locked("42", True, pin="000000")
        assert err.value.attempts_left == 4

    _run_control(backend, private, run)
    assert backend.commands == []


def test_locked_pin_is_not_tried(control):
    public, private = control
    backend = FakeControlBackend(public, [])
    backend.attempts = 0

    async def run(client):
        with pytest.raises(api_mod.MazdaPinError):
            await client.set_locked("42", True)

    _run_control(backend, private, run)
    assert "cma-app-car-control/api/security-code/check-code" not in backend.calls


def test_missing_pin(control):
    public, private = control
    backend = FakeControlBackend(public, [])

    async def run(client):
        with pytest.raises(api_mod.MazdaPinError):
            await client.set_locked("42", True)

    _run_control(backend, private, run, pin=None)
    assert backend.calls == []


def test_serial_for_other_key_needs_relogin(control, monkeypatch):
    public, _private = control
    _other_public, other_private = generate_key_pair()
    backend = FakeControlBackend(public, [])

    # Real RSA decryption with a mismatched key almost always raises ValueError,
    # but not guaranteed to (~1 in 65000 chance of accidentally valid padding) -
    # force it so the test is deterministic instead of occasionally flaky.
    def _decrypt_serial_fails(_serial: str, _private_key: str) -> str:
        raise ValueError("Decryption failed")

    monkeypatch.setattr(api_mod, "decrypt_serial", _decrypt_serial_fails)

    async def run(client):
        with pytest.raises(api_mod.MazdaAuthError):
            await client.set_locked("42", True)

    _run_control(backend, other_private, run)


def test_climate_on_signed_without_pin(control):
    public, private = control
    backend = FakeControlBackend(public, [-100, 0])

    async def run(client):
        await client.set_climate("42", True, 22.5)

    _run_control(backend, private, run, pin=None)
    body = backend.commands[0]
    assert body["enabled"] is True
    assert body["targetTemp"] == 225
    assert body["runTime"] == 15
    assert body["command"] == "air"
    assert "rcToken" not in body
    assert not any("security-code" in c for c in backend.calls)
    assert "cma-app-car-control/api/control/air-conditioner" in backend.calls


def test_climate_off(control):
    public, private = control
    backend = FakeControlBackend(public, [1015])  # already off counts as success

    async def run(client):
        await client.set_climate("42", False, 21)

    _run_control(backend, private, run)
    assert backend.commands[0]["enabled"] is False


def test_climate_temperature_range(control):
    public, private = control
    backend = FakeControlBackend(public, [])

    async def run(client):
        with pytest.raises(ValueError):
            await client.set_climate("42", True, 35)

    _run_control(backend, private, run)
    assert backend.calls == []


# ------------------------------------------------------- further commands


@pytest.mark.parametrize(
    ("call", "path", "params", "serial_type"),
    [
        (lambda c: c.request_status_update("42"), "control/condition-inquiry",
         {"command": "COMMAND_GET_NEW_CONDITION"}, "1"),
        (lambda c: c.set_charge_limit("42", 85), "charge/percentage",
         {"command": "charge_max", "chargePercentageMax": 85}, "2"),
        (lambda c: c.flash_and_honk("42"), "control/flashing-honking",
         {"command": "flash_bee", "type": 3}, "1"),
        (lambda c: c.set_seat("42", "heat", "driver", 2), "control/seats/heat",
         {"command": "seats_heat", "masterSwitch": 1, "masterLevel": 2}, "1"),
        (lambda c: c.set_seat("42", "wind", "passenger", 0), "control/seats/wind",
         {"command": "seats_wind", "copilotSwitch": 0}, "1"),
        (lambda c: c.set_steering_wheel_heat("42", True), "control/steering-wheel/heat",
         {"command": "steering_wheel_heating", "open": True}, "1"),
        (lambda c: c.set_defrost("42", False), "control/defrost",
         {"command": "defrost", "enabled": False}, "1"),
    ],
)
def test_commands_without_pin(control, call, path, params, serial_type):
    public, private = control
    backend = FakeControlBackend(public, [0])

    async def run(client):
        await call(client)

    _run_control(backend, private, run, pin=None)
    body = dict(backend.commands[0])
    for key in ("sign", "seriralNo", "vehicleId"):
        body.pop(key)
    assert body == params
    assert backend.command_paths == [path]
    assert backend.serial_types == [serial_type]
    assert not any("security-code" in c for c in backend.calls)


@pytest.mark.parametrize(
    ("call", "path", "params"),
    [
        (lambda c: c.set_windows("42", True), "control/windows", {"command": "window", "open": True}),
        (lambda c: c.set_trunk("42", False), "control/trunk", {"command": "trunk", "open": False}),
    ],
)
def test_commands_with_pin(control, call, path, params):
    public, private = control
    backend = FakeControlBackend(public, [0])

    async def run(client):
        await call(client)

    _run_control(backend, private, run)
    body = dict(backend.commands[0])
    assert body.pop("rcToken") == "rc-1"
    for key in ("sign", "seriralNo", "vehicleId"):
        body.pop(key)
    assert body == params
    assert backend.command_paths == [path]


def test_battery_preheat_plan(control):
    public, private = control
    backend = FakeControlBackend(public, [0, 0])

    async def run(client):
        plan = await client.get_battery_preheat_plan("42")
        assert plan["planId"] == 7
        await client.set_battery_preheat("42", plan, "20261006070000")
        await client.set_battery_preheat("42", plan, None)

    _run_control(backend, private, run)
    assert backend.command_paths == ["heating-plans/update-plan", "heating-plans/plan-availability"]
    assert backend.serial_types == ["5", "5"]
    assert backend.commands[0]["endData"] == "20261006070000"
    assert backend.commands[0]["planId"] == "7"
    assert backend.commands[1]["enabled"] is False


def test_charge_plan(control):
    public, private = control
    backend = FakeControlBackend(public, [0, 0])
    plan = {"planId": 5, "planType": 1, "timeFormat": 1, "timeZone": "GMT+02:00"}
    # what the car actually reports: no time zone or time format
    reported = {"planId": 6, "planType": 1, "isValid": 1, "startTime": "0100", "endSwitch": 1}

    async def run(client):
        await client.set_charge_plan("42", plan, start="2230", end="0600")
        await client.set_charge_plan("42", reported, start="0230", end="0615")
        with pytest.raises(ValueError):
            await client.set_charge_plan("42", plan, start="22:30", end="0600")
        with pytest.raises(ValueError):
            await client.set_charge_plan("42", {"timeZone": "GMT+02:00"}, start="2230", end="0600")

    _run_control(backend, private, run)
    first, second = backend.commands
    assert backend.command_paths == ["charge/modify-plan", "charge/modify-plan"]
    assert (first["startTime"], first["endTime"], first["endSwitch"], first["timeZone"]) == (
        "2230", "0600", 1, "GMT+02:00"
    )
    assert second["planId"] == "6"
    assert second["timeZone"] == api_mod.local_gmt_offset()
    assert backend.serial_types == ["2", "2"]


def test_charge_plan_lifecycle(control):
    public, private = control
    backend = FakeControlBackend(public, [0, 0, 0])

    async def run(client):
        await client.add_charge_plan("42", start="0100", end="0500")
        await client.set_charge_plan_enabled("42", "6", False)
        await client.delete_charge_plan("42", "6")
        with pytest.raises(ValueError):
            await client.add_charge_plan("42", start="0100", end="2460")

    _run_control(backend, private, run)
    assert backend.command_paths == ["charge/add-plan", "charge/validity", "charge/delete-plan"]
    added, validity, deleted = backend.commands
    assert (added["command"], added["startTime"], added["endTime"]) == ("add_charge_plan", "0100", "0500")
    assert added["timeZone"] == api_mod.local_gmt_offset()
    assert (validity["planId"], validity["enabled"]) == ("6", False)
    assert (deleted["command"], deleted["planId"]) == ("delete_charge_plan", "6")
    assert backend.serial_types == ["2", "2", "2"]


def test_value_checks_before_sending(control):
    public, private = control
    backend = FakeControlBackend(public, [])

    async def run(client):
        with pytest.raises(ValueError):
            await client.set_charge_limit("42", 50)
        with pytest.raises(ValueError):
            await client.set_seat("42", "heat", "driver", 4)

    _run_control(backend, private, run)
    assert backend.calls == []
