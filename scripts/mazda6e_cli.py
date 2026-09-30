#!/usr/bin/env python3
"""Test your Mazda 6e account without Home Assistant.

    pip install aiohttp cryptography
    python scripts/mazda6e_cli.py --email you@example.com

Logs in exactly like the integration, asks for the e-mail verification code
if Mazda requests one, and prints the parsed and raw vehicle status. Tokens
and the control key are cached in ~/.mazda6e_cli.json so repeated runs don't
trigger new codes.

    python scripts/mazda6e_cli.py --email you@example.com --lock
    python scripts/mazda6e_cli.py --email you@example.com --unlock

locks/unlocks the first car; the 6-digit control PIN is asked for.

    python scripts/mazda6e_cli.py --email you@example.com --climate-on 21
    python scripts/mazda6e_cli.py --email you@example.com --climate-off

starts/stops remote climate (no PIN needed).
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


def _load_cache(email: str) -> dict:
    try:
        return json.loads(CACHE.read_text()).get(email.lower(), {})
    except (OSError, ValueError):
        return {}


def _save_cache(email: str, data: dict) -> None:
    try:
        all_data = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        all_data = {}
    all_data[email.lower()] = data
    CACHE.write_text(json.dumps(all_data, indent=2))
    CACHE.chmod(0o600)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--region", default="europe", choices=["europe", "asia"])
    parser.add_argument("--raw", action="store_true", help="also print the raw JSON response")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--lock", action="store_true", help="lock the car")
    action.add_argument("--unlock", action="store_true", help="unlock the car")
    action.add_argument("--climate-on", type=float, metavar="TEMP", help="start climate at TEMP °C")
    action.add_argument("--climate-off", action="store_true", help="stop climate")
    args = parser.parse_args()

    cache = _load_cache(args.email)
    device_id = cache.get("device_id") or str(uuid.uuid4())
    private_key = cache.get("private_key")

    def remember(token: str, refresh: str) -> None:
        _save_cache(
            args.email,
            {
                "device_id": device_id,
                "token": token,
                "refresh_token": refresh,
                "private_key": client.private_key,
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
            private_key=private_key,
        )

        try:
            vehicles = await client.get_vehicles() if client.token else None
        except api.MazdaAuthError:
            vehicles = None

        if vehicles is None:
            password = getpass.getpass("Mazda password: ")
            public_key, client.private_key = crypto.generate_key_pair()
            if await client.login(args.email, password, public_key):
                await client.request_device_code(args.email)
                code = input(f"Verification code sent to {args.email}: ")
                await client.verify_device_code(args.email, code)
            vehicles = await client.get_vehicles()

        if not vehicles:
            print("No vehicles on this account.")
            return

        wants_climate = args.climate_on is not None or args.climate_off
        if (args.lock or args.unlock or wants_climate) and not client.private_key:
            print("No control key cached, delete ~/.mazda6e_cli.json and log in again.")
            return

        if wants_climate:
            vehicle = vehicles[0]
            on = args.climate_on is not None
            print(f"{'Starting' if on else 'Stopping'} climate for {vehicle.display_name} ...")
            await client.set_climate(vehicle.vehicle_id, on, args.climate_on if on else 21.0)
            print("Confirmed by the car.")
            return

        if args.lock or args.unlock:
            vehicle = vehicles[0]
            pin = getpass.getpass("Control PIN (6 digits): ")
            print(f"{'Locking' if args.lock else 'Unlocking'} {vehicle.display_name} ...")
            await client.set_locked(vehicle.vehicle_id, args.lock, pin=pin)
            print("Confirmed by the car.")
            return

        for vehicle in vehicles:
            print(f"\n=== {vehicle.display_name} (id {vehicle.vehicle_id}) ===")
            status = await client.get_status(vehicle.vehicle_id)
            parsed = asdict(status)
            raw = parsed.pop("raw")
            for key, value in parsed.items():
                print(f"  {key:26} {value}")
            print(f"  {'locked':26} {status.locked}")
            print(f"  {'any_door_open':26} {status.any_door_open}")
            if args.raw:
                print(json.dumps(raw, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
