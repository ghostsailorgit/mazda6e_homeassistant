"""Client tests against a local fake of the Mazda backend."""

import asyncio
import base64

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
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
