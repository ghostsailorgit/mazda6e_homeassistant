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
        if path.endswith("serial-no/get"):
            enc = self.public_key.encrypt(self.serial.encode(), padding.PKCS1v15())
            return web.json_response({"success": True, "data": base64.encodebytes(enc).decode()})
        if path.endswith("control/doors"):
            canonical = "&".join(
                f"{k}={str(v).lower() if isinstance(v, bool) else v}"
                for k, v in sorted(body.items())
                if k not in ("sign", "command")
            )
            self.public_key.verify(  # raises InvalidSignature -> HTTP 500
                base64.b64decode(body["sign"]), canonical.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            assert body["seriralNo"] == self.serial and body["rcToken"] == "rc-1"
            self.commands.append(body)
            return web.json_response({"success": True, "data": {"commandId": "cmd-1"}})
        if path.endswith("control/control-result"):
            assert body == {"commandId": "cmd-1", "vehicleId": "42"}
            code = self.results.pop(0)
            return web.json_response({"success": True, "data": {"resultCode": code, "errorMsg": "nope"}})
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


def test_serial_for_other_key_needs_relogin(control):
    public, _private = control
    _other_public, other_private = generate_key_pair()
    backend = FakeControlBackend(public, [])

    async def run(client):
        with pytest.raises(api_mod.MazdaAuthError):
            await client.set_locked("42", True)

    _run_control(backend, other_private, run)
