#!/usr/bin/env python3
"""Test and explore a Mazda 6e account without Home Assistant.

    pip install aiohttp cryptography
    python scripts/mazda6e_cli.py --email you@example.com login

Logs in exactly like the integration and asks for the e-mail verification
code if Mazda requests one. Tokens and the control key are cached in
~/.mazda6e_cli.json, so every other command runs without a password.

Mazda allows one active login per account: logging in here logs out the
phone app or Home Assistant if they use the same account.

Reading:
    status [--raw]                 parsed (and raw) vehicle status
    probe [--out DIR]              query every known read endpoint, save the
                                   raw answers to DIR and print what is new
    call PATH [JSON]               raw authenticated POST, e.g.
                                   call cma-app-user/api/vehicle/function-config '{"vehicleId": "1"}'

Commands (the car must confirm them, like in the app):
    lock | unlock                  asks for the 6-digit control PIN
                                   (or uses the one saved with login --save-pin)
    climate on TEMP | climate off
    charge-plan add HHMM HHMM | modify ID HHMM HHMM | delete ID | enable ID | disable ID

Exploring commands of the app:
    windows                        asks the car for a fresh status (~30 s) and
                                   prints open state and opening degree per window
    raw CONTROL [JSON] [--no-pin]  signed command to cma-app-car-control/api/control/CONTROL,
                                   e.g. raw windows '{"open": true, "openDegree": 100}'
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import sys
import types
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp

# Import the integration's modules without executing its Home Assistant
# __init__.py (which needs homeassistant installed).
_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "mazda6e"
_pkg = types.ModuleType("mazda6e")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["mazda6e"] = _pkg
api = importlib.import_module("mazda6e.api")
crypto = importlib.import_module("mazda6e.crypto")

CACHE = Path.home() / ".mazda6e_cli.json"


def _load_cache(email: str) -> dict[str, Any]:
    try:
        return json.loads(CACHE.read_text()).get(email.lower(), {})
    except (OSError, ValueError):
        return {}


def _save_cache(email: str, data: dict[str, Any]) -> None:
    try:
        all_data = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        all_data = {}
    all_data[email.lower()] = data
    CACHE.write_text(json.dumps(all_data, indent=2))
    CACHE.chmod(0o600)


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--region", default="europe", choices=["europe", "asia"])
    parser.add_argument("--vin", help="pick a car by (the end of) its VIN; default: the first car")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="log in and cache the tokens")
    login.add_argument(
        "--save-pin", action="store_true", help="also ask for the control PIN and cache it (file mode 600)"
    )
    status = sub.add_parser("status", help="print the vehicle status")
    status.add_argument("--raw", action="store_true", help="also print the raw JSON response")
    probe = sub.add_parser("probe", help="query every known read endpoint")
    probe.add_argument("--out", default="probe-output", help="directory for the raw answers")
    call = sub.add_parser("call", help="raw authenticated POST")
    call.add_argument("path")
    call.add_argument("body", nargs="?", default="{}")

    sub.add_parser("windows", help="fresh window status from the car")
    raw = sub.add_parser("raw", help="signed remote command (the car executes it)")
    raw.add_argument("control", help="e.g. windows, trunk, doors")
    raw.add_argument("params", nargs="?", default="{}", help="JSON body without vehicleId/rcToken/serial/sign")
    raw.add_argument("--no-pin", action="store_true", help="command does not need the control PIN")
    sub.add_parser("lock")
    sub.add_parser("unlock")
    climate = sub.add_parser("climate")
    climate.add_argument("state", choices=["on", "off"])
    climate.add_argument("temperature", nargs="?", type=float, default=21.0)

    charge = sub.add_parser("charge-plan").add_subparsers(dest="action", required=True)
    add = charge.add_parser("add")
    add.add_argument("start", help="HHMM")
    add.add_argument("end", help="HHMM")
    modify = charge.add_parser("modify")
    modify.add_argument("plan_id")
    modify.add_argument("start", help="HHMM")
    modify.add_argument("end", help="HHMM")
    for name in ("delete", "enable", "disable"):
        charge.add_parser(name).add_argument("plan_id")
    return parser


async def _login(client: Any, email: str) -> None:
    password = getpass.getpass("Mazda password: ")
    public_key, client.private_key = crypto.generate_key_pair()
    if await client.login(email, password, public_key):
        await client.request_device_code(email)
        code = input(f"Verification code sent to {email}: ")
        await client.verify_device_code(email, code)


def _pick(vehicles: list[Any], vin: str | None) -> Any:
    if not vin:
        return vehicles[0]
    for vehicle in vehicles:
        if vehicle.vin.upper().endswith(vin.upper()):
            return vehicle
    raise SystemExit(f"No car with a VIN ending in {vin}")


async def _probe(client: Any, vehicle: Any, out: Path) -> None:
    """Query every known read endpoint and save the raw answers."""
    out.mkdir(parents=True, exist_ok=True)
    vid = vehicle.vehicle_id

    def save(name: str, data: Any) -> None:
        (out / f"{name}.json").write_text(_dump(data), encoding="utf-8")

    raw_vehicles = await client.get_vehicles_raw()
    save("vehicles", raw_vehicles)
    raw_vehicle = next((v for v in raw_vehicles if v.get("vin") == vehicle.vin), {})
    print("Vehicle fields the integration does not use yet:")
    known = {"carId", "vehicleId", "vin", "modelName", "seriesName", "carName", "plateNumber"}
    for key, value in sorted(raw_vehicle.items()):
        if key not in known:
            print(f"  {key:28} {value if not isinstance(value, (dict, list)) else _dump(value)}")
    protocol = raw_vehicle.get("protocolType")
    print(f"\nTelemetry protocol: {protocol!r}" + (" -> MQTT telemetry is worth a try" if protocol and str(protocol).upper() == "MQTT" else ""))

    functions = await client.call("cma-app-user/api/vehicle/function-config", {"vehicleId": vid})
    save("function_config", functions)
    codes = ((functions.get("data") or {}).get("confList")) if isinstance(functions.get("data"), dict) else None
    print(f"\nFunctions the car reports ({len(codes or [])}):")
    for code in sorted(codes or []):
        print(f"  {code}")

    default = await client.get_status_raw(vid)
    full = await client.get_status_raw(vid, {key: True for key in api.STATUS_CRITERIA})
    save("condition_default", default)
    save("condition_all_sections", full)
    print("\nCondition sections that only appear when asked for:")
    extra = {k: v for k, v in full.items() if k not in default}
    if not extra:
        print("  (none)")
    for key, value in extra.items():
        print(f"  {key}: {_dump(value)}")

    for name, path, body in (
        ("heating_plans", "cma-app-car-control/api/heating-plans/query/list", {"vehicleId": vid}),
        ("pin_status", "cma-app-car-control/api/security-code/get-status", {}),
    ):
        answer = await client.call(path, body)
        save(name, answer)
        print(f"\n{name}: {_dump(answer.get('data') if answer.get('success') else answer)}")

    print(f"\nRaw answers saved to {out.resolve()}")


async def _run_command(client: Any, vehicle: Any, args: argparse.Namespace) -> Any:
    vid = vehicle.vehicle_id
    command = args.command
    if command in ("lock", "unlock"):
        pin = client.control_pin or getpass.getpass("Control PIN (6 digits): ")
        return await client.set_locked(vid, command == "lock", pin=pin)
    if command == "raw":
        pin = None
        if not args.no_pin and not client.control_pin:
            pin = getpass.getpass("Control PIN (6 digits): ")
        return await client.send_raw_command(
            vid, args.control, json.loads(args.params), needs_pin=not args.no_pin, pin=pin
        )
    if command == "climate":
        return await client.set_climate(vid, args.state == "on", args.temperature)
    if command == "charge-plan":
        if args.action == "add":
            return await client.add_charge_plan(vid, start=args.start, end=args.end)
        if args.action == "modify":
            status = await client.get_status_raw(vid)
            plans = (status.get("charge") or {}).get("chargePlanList") or []
            plan = next((p for p in plans if str(p.get("planId")) == args.plan_id), None)
            if plan is None:
                raise SystemExit(f"The car reports no charging plan {args.plan_id}")
            return await client.set_charge_plan(vid, plan, start=args.start, end=args.end)
        if args.action == "delete":
            return await client.delete_charge_plan(vid, args.plan_id)
        return await client.set_charge_plan_enabled(vid, args.plan_id, args.action == "enable")
    raise SystemExit(f"Unknown command {command}")


WINDOW_NAMES = ("rear left", "rear right", "front left", "front right")


async def _windows(client: Any, vehicle: Any) -> None:
    """Fresh window state: wake the car, wait for its upload, print the arrays."""
    before = (await client.get_status_raw(vehicle.vehicle_id)).get("lastUpdatedAt")
    await client.request_status_update(vehicle.vehicle_id)
    raw: dict[str, Any] = {}
    for _ in range(12):  # the car needs a while to upload
        await asyncio.sleep(5)
        raw = await client.get_status_raw(vehicle.vehicle_id)
        if raw.get("lastUpdatedAt") != before:
            break
    else:
        print("(the car sent no new status, showing the last one)")
    window = raw.get("window") or {}
    print(_dump(window))
    states, degrees = window.get("windows") or [], window.get("openDegree") or []
    for i, name in enumerate(WINDOW_NAMES):
        state = states[i] if i < len(states) else "?"
        degree = degrees[i] if i < len(degrees) else "?"
        print(f"  [{i}] {name:12} open={state}  degree={degree}")


async def main() -> None:
    args = _parser().parse_args()
    cache = _load_cache(args.email)
    device_id = cache.get("device_id") or str(uuid.uuid4())

    def remember(token: str, refresh: str) -> None:
        _save_cache(
            args.email,
            {
                "device_id": device_id,
                "token": token,
                "refresh_token": refresh,
                "private_key": client.private_key,
                "user_id": client.user_id,
                "control_pin": client.control_pin,
            },
        )

    async with aiohttp.ClientSession() as session:
        client = api.Mazda6eClient(
            session,
            args.region,
            device_id,
            token=cache.get("token"),
            refresh_token=cache.get("refresh_token"),
            on_token_update=remember,
            private_key=cache.get("private_key"),
            control_pin=cache.get("control_pin"),
        )
        client.user_id = cache.get("user_id")

        if args.command == "login":
            await _login(client, args.email)
            if args.save_pin:
                client.control_pin = getpass.getpass("Control PIN (6 digits) to cache: ")
            remember(client.token, client.refresh_token)
            vehicles = await client.get_vehicles()
            print(f"Logged in, {len(vehicles)} car(s): " + ", ".join(v.display_name for v in vehicles))
            return

        if not client.token:
            raise SystemExit("Not logged in, run the login command first.")
        try:
            vehicles = await client.get_vehicles()
        except api.MazdaAuthError as err:
            raise SystemExit(f"Session no longer valid ({err}), run the login command again.") from err
        if not vehicles:
            raise SystemExit("No vehicles on this account.")

        if args.command == "call":
            print(_dump(await client.call(args.path, json.loads(args.body))))
            return

        vehicle = _pick(vehicles, args.vin)
        if args.command == "probe":
            await _probe(client, vehicle, Path(args.out))
            return
        if args.command == "windows":
            await _windows(client, vehicle)
            return
        if args.command == "status":
            print(f"=== {vehicle.display_name} (id {vehicle.vehicle_id}) ===")
            status = await client.get_status(vehicle.vehicle_id)
            parsed = asdict(status)
            raw = parsed.pop("raw")
            for key, value in parsed.items():
                print(f"  {key:26} {value}")
            print(f"  {'locked':26} {status.locked}")
            if args.raw:
                print(_dump(raw))
            return

        if not client.private_key:
            raise SystemExit("No control key cached, run the login command again.")
        print(f"Sending {args.command} to {vehicle.display_name} ...")
        result = await _run_command(client, vehicle, args)
        print("Confirmed by the car." + (f"\n{_dump(result)}" if result else ""))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except api.MazdaError as err:
        raise SystemExit(f"{type(err).__name__}: {err}") from err
