🇩🇪 **Deutsche Version:** [README.de.md](README.de.md)

<p align="center"><img src="https://raw.githubusercontent.com/ghostsailorgit/mazda6e_homeassistant/main/custom_components/mazda6e/brand/icon@2x.png" alt="Mazda 6e for Home Assistant" width="200"></p>

# Mazda 6e for Home Assistant

Custom integration that pulls data for a **Mazda 6e** (and CX-6e) from the cloud
backend used by the official **"MAZDA 6e & CX-6e"** app and shows it in Home Assistant.

> Unofficial, not supported by Mazda. The API is undocumented and can change at any time.

## Compatibility

| | |
| --- | --- |
| Tested | Mazda 6e, region Europe, Home Assistant 2026.9 |
| Should work, not tested yet | Mazda CX-6e (same app and cloud), region Asia/Pacific |
| Requires | Home Assistant 2025.4 or newer, an account for the "MAZDA 6e & CX-6e" app |

If you use a CX-6e or the Asia/Pacific region, please
[open an issue](https://github.com/ghostsailorgit/mazda6e_homeassistant/issues/new/choose)
with the diagnostics download — even if everything works, so this table can be updated.

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → *Custom repositories* →
   `https://github.com/ghostsailorgit/mazda6e_homeassistant`, category *Integration*.
2. Install "Mazda 6e", restart Home Assistant.

Once the integration is included in the HACS default list, step 1 is no longer
needed: just search for "Mazda 6e" in HACS.

### Manual

Copy the `custom_components/mazda6e` folder into `<config>/custom_components/` and
restart Home Assistant.

## Setup

*Settings → Devices & Services → Add Integration → Mazda 6e*

1. Enter email, password, and region (the same credentials as the app).
2. Mazda sends a **confirmation code by email** — enter it.

The password is not stored, only the tokens and the control key. Under *Configure*
you can set the polling interval (default: 5 minutes).

> Mazda only allows one active login per account: if Home Assistant logs in, the
> phone app gets logged out (and vice versa). The fix is a **second account just for
> Home Assistant**:
>
> 1. Create a new account in the app with a different email address.
> 2. With your main account, open the vehicle in the app, tap *Share*, and invite
>    the new account.
> 3. Log in to the app with the new account and accept the vehicle share.
> 4. Set up a separate control PIN on the new account (e.g. by trying to open a
>    window — the app will then ask for one). The PIN belongs to the account; your
>    main account's PIN doesn't apply to the second account.
> 5. Switch the app back to your main account. From now on, only Home Assistant
>    uses the second account.

## What it shows

| Area | Entities |
| --- | --- |
| Battery | Battery level (%), range, charge limit |
| Charging | Charging current (sum of phases), AC charging current/battery current (disabled by default), charging status, remaining charge time, charging cable plugged in, charging |
| Open/closed | Lock, doors (overall + individual), trunk, hood, windows (overall + individual) |
| Other | Odometer, interior temperature, climate control on, vehicle state (driving/parked), tire pressure, online status, time of last update |

> Location is not shown: the cloud API does not report GPS coordinates for this car
> (the official app doesn't show a location either), so there is no `device_tracker`
> entity.

Note on the lock: the entity uses the *lock* device class, so **"on" (unlocked) /
"off" (locked)** — the inverse of what you might expect from the name.

## Remote lock

The **`lock.<car>_doors`** entity locks/unlocks the car remotely, just like the lock
button in the app. It needs the **6-digit control PIN** from the Mazda app. Two modes:

- **Don't store the PIN (default):** Home Assistant asks for the PIN on every
  lock/unlock action.
- **Store the PIN:** *Integration → Configure → "Store control PIN"* and enter the
  PIN. It's verified against Mazda immediately when you save it. After that,
  automations work too (e.g. "lock at 10pm if unlocked"). Caution: anyone with
  access to Home Assistant can then unlock the car.

Behind the scenes (same as the app): check remaining PIN attempts → exchange the PIN
for an `rcToken` → fetch an encrypted one-time serial number and decrypt it with the
key registered at login → sign the command with RSA-SHA256 and send it to
`control/doors` → poll `control/control-result` until the car confirms (max. 90 s).

Safety net: if no PIN attempts are left, the PIN is never sent at all. On a wrong PIN
the error message shows the remaining attempts.

After a confirmed command the entity shows the new state immediately, until the car
itself reports a newer status (after 10 minutes at the latest).

> The car needs mobile network coverage. In underground garages a command can fail
> with "not confirmed in time".

## Climate control

The **"Climate control"** entity (`climate.<car>_climate`) starts and stops standalone
climate control, just like the fan button in the app — e.g. to preheat the car in
winter or precool it in summer.

- Modes: *Off* and *Heat/Cool*, target temperature 16–30 °C in 0.5° steps
- Climate control always runs for **15 minutes** (the app's default)
- No PIN needed (the app doesn't ask for one either)
- Changing the temperature while it's off only stores the value and starts
  nothing — only "turn on" starts it with that temperature
- Current temperature = the interior temperature the car reports

Example automation: preheat to 21 °C at 7:15am on workdays

```yaml
automation:
  - alias: Preheat Mazda
    triggers:
      - trigger: time
        at: "07:15:00"
    conditions:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.mazda_6e_climate
        data:
          temperature: 21
          hvac_mode: heat_cool
```

## More remote controls

All commands are signed the same way as locking. Entities for equipment the car
doesn't have (per its `function-config` feature list) are not created.

| Entity | What it does | PIN |
| --- | --- | --- |
| Button **Request status update** | wakes the car so it reports fresh data immediately (re-polled after ~30 s) | – |
| Button **Flash lights** / **Flash and honk** | find the car in a parking lot | – |
| Slider **Set charge limit** | 60–100 % | – |
| Select **Seat heating / seat ventilation driver & passenger** | off, level 1–3 | – |
| Switch **Steering wheel heating**, **Defrost windscreen** | on/off | – |
| Cover **Windows ventilation**, **Tailgate** | open/close | stored PIN required |
| Switch + time **Battery preheating (car plan)** | the preheat plan also shown in the app | – |
| Switch + start/end **Charging schedule (car plan)** | the charging plan from the app (only if one exists) | – |

Windows and the tailgate don't prompt for a PIN in Home Assistant (covers can't do
that) — they only work with a stored control PIN, otherwise you get an error.

> **Windows only open a gap.** Over Mazda's cloud the car only offers the
> ventilation position: the front windows open to about 10 %. Opening all windows
> fully and raising/lowering the rear spoiler only work over Bluetooth (the app's
> digital key, function codes `#windowOpenBT` / `#SpoilerRaiseLowerBT`) — the app
> also only opens a gap and hides the spoiler button when the phone isn't connected
> to the car. Home Assistant cannot do this.

Additional sensors: interior humidity and PM2.5, lights (low/high beam, position
lights, turn indicators — disabled by default).

## Pre-conditioning with departure plans

The integration can preheat or precool the car before departure — with **as many
departure plans as you need** and/or triggered by **any Home Assistant trigger**.
The plans live in Home Assistant (not in the car) and still work if the cloud is
briefly unreachable.

**Departure plans:** *Settings → Devices & Services → Mazda 6e → Add departure plan*.
Each plan has a name, a departure time, the weekdays it applies to and its own
temperature — e.g. "Work, 07:30, Mon–Fri, 21 °C" and "Gym, 17:00, Tue+Thu, 19 °C".
Several plans may fall on the same day. Each plan is its own device below the car
(e.g. *Mazda 6e Work*) with three entities for the dashboard: *Departure plan* (on/off),
*Departure time* and *Temperature*; the weekdays are changed with *Reconfigure* on the
plan. The main switch *Pre-conditioning departure plans* turns all plans on or off.

**Profile** (shared by all plans, under *Configure* on the device):

- *Pre-conditioning temperature* (16–30 °C) — used when starting with the button or
  the service; plans use their own temperature
- *Pre-conditioning lead time* (5–30 min) — how long before departure it starts;
  climate control runs for exactly that long
- *Pre-conditioning: seat heating* (driver's seat, off/1–3)
- *Pre-conditioning: steering wheel heating*, *…: defrost*
- *Pre-conditioning: battery preheating* — always sets the car's own battery preheat
  plan to the next planned departure (the battery needs more lead time, which the
  car manages itself)
- *Pre-conditioning: weather source* — pick a `weather.*` entity to only pre-condition
  when needed; *off* (default) always pre-conditions
- *Pre-conditioning: minimum temperature* — with a weather source selected, a plan
  only runs if the hourly forecast for the departure time is below this value. A
  weather lookup that fails (integration hiccup, no forecast data) never blocks
  pre-conditioning — it runs as if no threshold were set

**Display:** *Next departure* and *Next pre-conditioning start* (timestamps; the
attributes show the plan, the last run and any failed steps) and *Forecast at
departure* — the hourly forecast of the weather source for the next departure,
refreshed every 30 minutes and whenever the weather entity changes. If the departure lies beyond the hourly forecast
(usually ~2 days), it shows the day's forecast low instead (attribute
`forecast_type`: `hourly` / `daily`); unknown without a weather source or forecast.
The weather gate itself always uses the hourly forecast.

> **Upgrading from 0.7 or older:** the per-weekday switches and times are replaced by
> departure plans. Your settings are converted automatically (days with the same
> time become one plan, e.g. "07:30, Tue–Fri"); dashboards and automations that used
> the old *Pre-conditioning Monday…* / *Departure Monday…* entities have to be
> updated. Requires Home Assistant 2025.4 or newer.

**Buttons:** *Start pre-conditioning* / *stop* (immediately, with the profile) and
*Skip next departure* (e.g. public holiday, working from home). To undo a skip, press
*Undo skip* (only available while a departure is skipped) or switch the plan of the
skipped departure — or all departure plans — off and on again.

### External triggers (automations)

| Service | Purpose |
| --- | --- |
| `mazda6e.start_preconditioning` | pre-condition now; optional `temperature`, `duration`, `seat_heat` (0–3), `steering_wheel`, `defrost`, `battery` — empty fields use the profile |
| `mazda6e.stop_preconditioning` | turns off climate, seat/steering wheel heating, and defrost |
| `mazda6e.skip_next_departure` | skip the next departure of the departure plans |
| `mazda6e.send_raw_command` | diagnostics, admins only: sends any signed remote command (`control`, e.g. `windows`, and `params`) and returns the car's answer — for finding out parameters the app uses. The car really executes it |

`device_id` is only needed with multiple cars. `start_preconditioning` returns the
failed steps (`failed`) in its response. Every run fires the `mazda6e_preconditioning`
event (`action`: `started`/`stopped`/`skipped`, `source`: `schedule`/`service`/`button`,
`departure`, `plan`, `failed`).

Example: preheat based on your calendar, only when it's cold

```yaml
automation:
  - alias: Preheat Mazda before appointments
    triggers:
      - trigger: calendar
        event: start
        entity_id: calendar.work
        offset: "-0:20:00"
    conditions:
      - condition: numeric_state
        entity_id: sensor.outdoor_temperature
        below: 5
    actions:
      - action: mazda6e.start_preconditioning
        data:
          temperature: 22
          duration: 20
          seat_heat: 2
          steering_wheel: true
          defrost: true
```

Example: skip the departure plans on public holidays

```yaml
automation:
  - alias: Skip Mazda departure on public holidays
    triggers:
      - trigger: time
        at: "20:00:00"
    conditions:
      - condition: state
        entity_id: binary_sensor.public_holiday_tomorrow
        state: "on"
    actions:
      - action: mazda6e.skip_next_departure
```

Example: notify when a step fails

```yaml
automation:
  - alias: Mazda pre-conditioning failed
    triggers:
      - trigger: event
        event_type: mazda6e_preconditioning
    conditions:
      - "{{ trigger.event.data.failed | length > 0 }}"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Pre-conditioning failed: {{ trigger.event.data.failed | join(', ') }}"
```

## Testing an account without Home Assistant

```bash
pip install aiohttp cryptography
python scripts/mazda6e_cli.py --email you@example.com login
python scripts/mazda6e_cli.py --email you@example.com status --raw
```

`login` asks for the password and, if needed, the email code, and caches the
session in `~/.mazda6e_cli.json`; every other command runs without a password.
Logging in here logs out the app or Home Assistant if they use the same account.

| Command | What it does |
| --- | --- |
| `status [--raw]` | parsed (and raw) vehicle status |
| `probe [--out DIR]` | queries every known read endpoint, saves the raw answers and prints fields the integration doesn't use yet — attach the output when reporting a car that behaves differently |
| `call PATH [JSON]` | raw authenticated request, for exploring the API |
| `lock` / `unlock` | remote locking (asks for the control PIN, or uses the one cached with `login --save-pin`) |
| `windows` | asks the car for a fresh status and prints open state and opening degree per window |
| `raw CONTROL [JSON] [--no-pin]` | signed remote command, e.g. `raw windows '{"command": "window", "open": false}'` — for finding out parameters; the car really executes it |
| `climate on 21` / `climate off` | climate control |
| `charge-plan add 2300 0600` | creates a charging schedule; `modify ID HHMM HHMM`, `enable ID`, `disable ID`, `delete ID` change it |

## Troubleshooting

- Enable debug logging:
  ```yaml
  logger:
    logs:
      custom_components.mazda6e: debug
  ```
- *Download diagnostics* on the integration page gives you the vehicle's raw data
  (VIN and tokens are redacted). Handy for mapping unknown fields.
- Still stuck? [Open an issue](https://github.com/ghostsailorgit/mazda6e_homeassistant/issues/new/choose)
  with the diagnostics file attached — never post your password, PIN or tokens.

## Background (research)

The Mazda 6e is built on a Changan platform (Deepal SL03). The app therefore does
**not** talk to the old MyMazda backend (the one used by `pymazda`), but to Changan's
"CMA" gateway:

| Region | Base URL |
| --- | --- |
| Europe | `https://cma-m.iov.changanauto.com.de/cma-app-gw` |
| Asia/Pacific | `https://cma.iov.changanauto.sg/cma-app-gw` |

Flow, same as the app:

1. `cma-app-auth/api/login/email-pass-in/v2` – email and password are encrypted with
   an RSA key embedded in the app; the client also registers its own public key
   (`pubKey`).
2. For a new device (`emailVerify: true`), Mazda sends a code by email
   (`send-email/device-login/send`), confirmed via `login-device/email-verify`.
3. Vehicle list: `cma-app-user/api/car/vehicles` (or `vehicle/vehicles`)
4. Status: `cma-app-car-condition/api/vehicle/condition/v2`
5. Token refresh: `cma-app-auth/api/auth/refresh-token`

The protocol details come from the existing community projects
[fano0001/home-assistant-mazda-6e](https://github.com/fano0001/home-assistant-mazda-6e) (Apache-2.0)
and [Sunek0/ha-mazda-6e-cx6](https://github.com/Sunek0/ha-mazda-6e-cx6) (MIT).

### Protocol uncertainties

The two reference projects disagree on a few details; this is what's implemented:

- Honk/flash: `type` 1 = light only, 3 = light + horn (per Sunek0; fano uses 1 for
  "find my car") — hence two separate buttons
- Windows: without `openType` (fano). With every `openType` tried (0–11, 20, 50, 99–101, 255, -1, incl. Sunek0's
  10) a Mazda 6e in Europe rejects opening with "The Controller Is Not Responding".
  The `windows`/`openDegree` arrays of the status list the rear windows first
- Signature: no empty `rcToken` for commands that don't need a PIN

If any of this doesn't work on your actual car, please report it with a debug log.

## Development

```bash
# API client and data parsing (no Home Assistant)
pip install aiohttp cryptography pytest
pytest tests --ignore tests/ha

# Home Assistant part (config flow, entities) – separate venv, Python 3.13
pip install -r requirements_test.txt
pytest tests/ha
```

The two suites run separately because the HA test plugin globally blocks network
sockets that the API tests' fake server needs.
