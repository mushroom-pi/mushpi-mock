# 🍄 Mushroom Pi 🍓 - Pico Growing Units Simulator

![Python](https://img.shields.io/badge/python-%233670A0.svg?style=for-the-badge&logo=python&logoColor=ffdd54)![uv](https://img.shields.io/badge/uv-%23DE5FE9.svg?style=for-the-badge&logo=uv&logoColor=white)![FastAPI](https://img.shields.io/badge/fastapi-%23009688.svg?style=for-the-badge&logo=fastapi&logoColor=white)![Pydantic](https://img.shields.io/badge/pydantic-%23E92063.svg?style=for-the-badge&logo=pydantic&logoColor=white)![Git](https://img.shields.io/badge/git-%23F05033.svg?style=for-the-badge&logo=git&logoColor=white)

Mock Pico growing units — mirrors the [`mushpi-grow`](https://github.com/mushroom-pi/mushpi-grow) firmware's REST API with synthetic sensor data. Use for development, demos, screenshots, and testing without real hardware.

Each mock unit runs as an independent FastAPI server on its own port, announces itself to the server on startup, and generates **hysteresis-simulated sensor readings** matching the real Pico's control loop behavior — sawtooth oscillation around setpoints, not random noise.

## Quick Start

```bash
# Install dependencies
uv sync

# Start a single mock unit on port 5000
uv run mushpi-mock start

# Start 3 units with custom handles
uv run mushpi-mock start --count 3 --handles grow-left,grow-center,grow-right

# Seed 14 days of backdated data for instant demos
uv run mushpi-mock start --count 3 --seed-days 14

# Use a config file
uv run mushpi-mock start --config config.example.json
```

## How It Works

1. **Start**: The CLI spawns one or more uvicorn servers, each on its own port.
2. **Announce**: Each unit `POST`s to `http://localhost:3000/v1/pico-units/announce` (configured in `config.json`). Retries with exponential backoff if the server isn't ready. Returns the `pico_unit_id` assigned by the server. The identity it reports is **per unit, not per process**: each unit's MAC is derived from its handle (`02:` + the first five bytes of `sha256(handle)` — a locally-administered, unicast address that is unique per unit and stable across restarts), so N units sharing one process look like N distinct boards. The same value is what `GET /` and `GET /system` serve under `wifi.mac`, so the hub's announce-time identity and its poll-time identity can never disagree.
3. **Re-announce**: A unit that announced but then stops being polled re-announces itself, so a lost address on the hub side is recoverable (a real Pico announces at boot only). If no `GET /` has arrived for **3 minutes** (three poll intervals) the unit re-announces with the same backoff, and never more than **once per 5 minutes** while it stays dark. A unit that *is* being polled never re-announces: the hub re-applies the active batch's setpoints on every announce, so re-announcing a healthy unit would fight the grower's manual overrides.
4. **Simulate**: A hysteresis control loop (3-zone humidity, 2-zone temperature) produces a characteristic sawtooth oscillation around setpoints — matching the real Pico firmware's behavior. Humidifier ON drives humidity up, fan ON drives it down, heater ON drives temperature up.
5. **Seed** (optional): `--seed-days N` bulk-inserts N days of backdated readings into the server's SQLite database via Python's `sqlite3` stdlib. All 17 columns populated — temperature, humidity, relay states, setpoints, board metrics, response time. Requires `db_path` in `config.json` and a successful announce.
6. **Serve**: The FastAPI app exposes all Pico endpoints — `GET /`, `/sensors`, `/setpoints`, `/outputs`, `/control`, `/setup`, `/health`, `/system`, plus `POST`/`PUT` mutations — and one mock-only endpoint, `GET /version`, which separates the mock's own tool version from the firmware/API versions it represents.
7. **Poll**: The server polls `GET /` every 60 seconds (same as with real Pico units) and stores readings in SQLite. Every such poll also resets the unit's re-announce timer (step 3).

## Runtime State — Survives a Stop/Start

The mock **persists grower-chosen control state across a stop/start of the process**, mirroring the firmware's `runtime.json` on flash:

| What | Survives stop/start? |
|---|---|
| Setpoints (targets) | **Yes** — restored on start |
| Control-loop on/off flag | **Yes** — the loop restarts as it was left |
| Hand-set fan + humidifier positions | **Yes, when the loop is off** — restored as you left them |
| **Heater** | **Never** — always comes back **off** after a restart (same safety rule as the device) |
| Pin mapping (`POST /setup`) | No — memory-only here; the device writes it to `config.json` and keeps it across reboots |
| Hysteresis config (`period_s`, `hyst_*`, …) | No — the device loads these from `config.json` at boot; they are not runtime state |

State lives in **one JSON file per unit handle** (`<handle>.json`, percent-encoded for filesystem safety, so two handles never collide) under the state directory. Files are written atomically (`.tmp` + `os.replace`). A missing or corrupt state file is never fatal: the unit starts with its defaults.

Configure the directory with the top-level `state_path` config key or the `--state-path` CLI flag (the flag overrides the config file). Default when unset: `~/.mushpi-mock/`.

```json
// config.json
{ "state_path": "~/.mushpi-mock" }
```

```bash
uv run mushpi-mock start --state-path /var/lib/mushpi-mock/state
```

To reset a unit to factory defaults, stop the mock and delete its state file. (A real unit needs the equivalent flash wipe; the mock's durability is a file on this machine — labelled on the unit page's simulated-unit banner.)

## Announce IP

By default each unit announces itself to the server with `ip: 127.0.0.1` (and reports the same address in `GET /system` under `wifi.ip`). That works when the server runs on the same host — but not when it doesn't, because `127.0.0.1` always resolves to the *server's own* loopback.

Configure the announced address with the top-level `announce_ip` config key or the `--announce-ip` CLI flag (the flag overrides the config file; all units share the value since they run in one process/container):

```json
// config.json
{ "announce_ip": "172.17.0.1", "server_url": "http://localhost:3000" }
```

```bash
uv run mushpi-mock start --announce-ip 172.17.0.1
```

> ⚠️ **The value must be a valid IPv4 address.** The server validates the announce payload's `ip` field with `@IsIP('4')` and rejects invalid payloads with **HTTP 422** — its global `ValidationPipe` runs with `forbidNonWhitelisted` and an `UnprocessableEntityException` factory, so hostnames like `host.docker.internal` *and* unknown keys (e.g. a stale `software_version`) both fail there.

Concrete use case — mock talking to a **Dockerized server**: set `announce_ip` to the Docker bridge gateway IP the container can use to reach the host (e.g. `172.17.0.1` or `172.18.0.1` — find it with `docker network inspect <network>`). If the mock is *itself* containerized on a user-defined network, set a static IP reachable from the server container instead.

## Architecture

```
mushpi-mock (Python + FastAPI + uv)
    ├── main.py           FastAPI app factory + click CLI (like main.ts + AppModule)
    ├── config.py         Pydantic config models (like ConfigModule + Joi)
    ├── state.py          In-memory unit state (like an @Injectable() service)
    ├── runtime_state.py  Per-handle state file: survives a stop/start (the device's runtime.json mirror)
    ├── router.py         All Pico API endpoints (like a @Controller())
    ├── unit_page.py      Vendored unit page + mock-only render seam
    ├── pins.py           Reserved-GPIO rules mirrored from grow
    ├── simulation.py     Hysteresis control loop (like a @Cron() service)
    ├── seed.py           Direct SQLite bulk seeding for --seed-days
    ├── announce.py       Server announce + poll-aware re-announce watchdog (like a bootstrap hook + @Interval)
    └── static/           Byte-identical vendored page assets + VENDOR manifest
```

## Endpoints

All endpoints mirror the real Pico firmware. See [`mushpi-grow/spec/openapi.yaml`](https://github.com/mushroom-pi/mushpi-grow/blob/dev/spec/openapi.yaml) for the canonical API contract. The provisioning surface is mirrored too: `GET /provision` serves the firmware's Wi-Fi setup form (the same bytes a connected unit serves), and `POST /provision` answers with the firmware's connected-(STA)-unit refusal — `403` before any state change — since the mock has no AP mode.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Full JSON system snapshot (server polls this every 60s); a browser whose `Accept` header prefers HTML gets the unit page instead |
| GET | `/sensors` | DHT sensor reading |
| GET | `/setpoints` | Current target temp/humidity |
| GET | `/outputs` | Relay states (fan, humidifier, heater) |
| GET | `/control` | Control loop enabled/disabled |
| GET | `/setup` | GPIO pin mapping |
| GET | `/health` | System health (RAM, WiFi, uptime) |
| GET | `/system` | Board info, MicroPython version, software version |
| GET | `/ui` | On-unit status-and-control page (HTML). The same page a real unit serves, so the interface can be seen and exercised without hardware |
| GET | `/provision` | Wi-Fi setup form (HTML) — the same page a real unit serves in both modes; submitting it below gets the firmware's STA refusal |
| GET | `/static/{filename}` | Page assets for the unit page: `unit.js` and `favicon.svg`. Unknown or missing asset → `404` |
| GET | `/ping` | Liveness check (empty `200`) |
| POST/PUT | `/setpoints` | Update temp/humidity targets |
| POST/PUT | `/outputs` | Set relay states |
| POST/PUT | `/control` | Enable/disable control loop |
| POST/PUT | `/setup` | Update GPIO configuration |
| POST | `/provision` | Wi-Fi credentials — refused with the firmware's exact STA-mode body (`403 {"ok": false, "error": "provisioning is only available in AP mode"}`), no AP path exists here |
| POST | `/reboot` | Mock reboot (no-op — returns 200) |
| GET | `/version` | **Mock-only** (not in the Pico spec): `{mock_version, represented_firmware_version, represented_api_version}` |

> The page is byte-identical to a unit's page except for the simulated-unit labels: grower-chosen values (targets, loop flag, hand-set fan/humidifier) persist across a stop/start via a state file rather than flash — the heater always comes back off — and the PIN mapping is memory-only here (see [Runtime State](#runtime-state--survives-a-stopstart)).

## Version Alignment

The mock carries **two version identities** (see `mushpi-hub/MAINTAINING.md` §Version & Release Semantics — Mock dual-versioning):

| Identity | Where it lives | Rule |
|---|---|---|
| **Represented firmware version** | `src/mushpi_mock/VERSION` + `_SOFTWARE_VERSION` in [`mushpi-grow/app/state.py`](https://github.com/mushroom-pi/mushpi-grow/blob/dev/app/state.py) (`state.py` mirror) | Must always equal grow's `_SOFTWARE_VERSION` — this is what `GET /system`, `GET /` (incl. its `firmware_version` key), and the announce payload report. |
| **Represented API contract version** | `_API_VERSION` in `src/mushpi_mock/state.py` | Must equal grow's `_API_VERSION` — the Pico↔Server REST contract generation reported in `GET /` and the announce payload (`api_version`). Bumped only on breaking contract changes. |
| **Mock's own service version** | `pyproject.toml` `version` (exposed via `importlib.metadata` as `mushpi_mock.__version__`) | Independent SemVer for the mock tool itself — surfaced **only** through `GET /version`, never on the mirrored Pico endpoints. |

`GET /version` answers all three at once, so a demo can prove exactly which firmware the mock is emulating without confusing it with the tool's own release line:

```json
{ "mock_version": "0.2.0", "represented_firmware_version": "0.8.4", "represented_api_version": 1 }
```

Verify the alignment with:

```bash
./scripts/check-version-alignment.sh
```

It checks the firmware version three-way (grow `_SOFTWARE_VERSION` vs `VERSION` vs `state.py`) and the API version two-way (grow `_API_VERSION` vs `state.py`), printing one pass/fail line each. Exit 0 = all aligned, 1 = mismatch / extraction failure, 2 = grow checkout not found. If you manually change the firmware version, also update `src/mushpi_mock/VERSION` and the `_SOFTWARE_VERSION` constant in `state.py`; if grow's `_API_VERSION` moves, update `state.py::_API_VERSION` in the same pass.

## Verification

```bash
# Start a mock unit in the background
uv run mushpi-mock start --count 1 --handles test-unit --base-port 5001 &

# Test sensor reading
curl -s http://localhost:5001/sensors | python -m json.tool

# Test setpoint update
curl -s -X PUT http://localhost:5001/setpoints \
  -H 'Content-Type: application/json' \
  -d '{"temp": 25, "humidity": 80}' | python -m json.tool

# Test full snapshot (what the server polls)
curl -s http://localhost:5001/ | python -m json.tool

kill %1
```

## License

Distributed under the MIT License — see [`LICENSE`](LICENSE).

Copyright (c) 2026 [Adriana Martín de Aguilera](https://www.amda.dev)
