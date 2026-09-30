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
2. **Announce**: Each unit `POST`s to `http://localhost:3000/v1/pico-units/announce` (configured in `config.json`). Retries with exponential backoff if the server isn't ready. Returns the `pico_unit_id` assigned by the server.
3. **Simulate**: A hysteresis control loop (3-zone humidity, 2-zone temperature) produces a characteristic sawtooth oscillation around setpoints — matching the real Pico firmware's behavior. Humidifier ON drives humidity up, fan ON drives it down, heater ON drives temperature up.
4. **Seed** (optional): `--seed-days N` bulk-inserts N days of backdated readings into the server's SQLite database via Python's `sqlite3` stdlib. All 17 columns populated — temperature, humidity, relay states, setpoints, board metrics, response time. Requires `db_path` in `config.json` and a successful announce.
5. **Serve**: The FastAPI app exposes all Pico endpoints — `GET /`, `/sensors`, `/setpoints`, `/outputs`, `/control`, `/setup`, `/health`, `/system`, plus `POST`/`PUT` mutations — and one mock-only endpoint, `GET /version`, which separates the mock's own tool version from the firmware/API versions it represents.
6. **Poll**: The server polls `GET /` every 60 seconds (same as with real Pico units) and stores readings in SQLite.

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
    ├── main.py         FastAPI app factory + click CLI (like main.ts + AppModule)
    ├── config.py       Pydantic config models (like ConfigModule + Joi)
    ├── state.py        In-memory unit state (like an @Injectable() service)
    ├── router.py       All Pico API endpoints (like a @Controller())
    ├── simulation.py   Hysteresis control loop (like a @Cron() service)
    ├── seed.py         Direct SQLite bulk seeding for --seed-days
    └── announce.py     Server announce on startup (like a bootstrap hook)
```

## Endpoints

All endpoints mirror the real Pico firmware. See [`mushpi-grow/spec/openapi.yaml`](https://github.com/mushroom-pi/mushpi-grow/blob/dev/spec/openapi.yaml) for the canonical API contract.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Full system snapshot (server polls this every 60s) |
| GET | `/sensors` | DHT sensor reading |
| GET | `/setpoints` | Current target temp/humidity |
| GET | `/outputs` | Relay states (fan, humidifier, heater) |
| GET | `/control` | Control loop enabled/disabled |
| GET | `/setup` | GPIO pin mapping |
| GET | `/health` | System health (RAM, WiFi, uptime) |
| GET | `/system` | Board info, MicroPython version, software version |
| POST/PUT | `/setpoints` | Update temp/humidity targets |
| POST/PUT | `/outputs` | Set relay states |
| POST/PUT | `/control` | Enable/disable control loop |
| POST/PUT | `/setup` | Update GPIO configuration |
| POST | `/reboot` | Mock reboot (no-op — returns 200) |
| GET | `/version` | **Mock-only** (not in the Pico spec): `{mock_version, represented_firmware_version, represented_api_version}` |

## Version Alignment

The mock carries **two version identities** (see `mushpi-ops/MAINTAINING.md` §Version & Release Semantics — Mock dual-versioning):

| Identity | Where it lives | Rule |
|---|---|---|
| **Represented firmware version** | `src/mushpi_mock/VERSION` + `_SOFTWARE_VERSION` in [`mushpi-grow/app/state.py`](https://github.com/mushroom-pi/mushpi-grow/blob/dev/app/state.py) (`state.py` mirror) | Must always equal grow's `_SOFTWARE_VERSION` — this is what `GET /system`, `GET /` (incl. its `firmware_version` key), and the announce payload report. |
| **Represented API contract version** | `_API_VERSION` in `src/mushpi_mock/state.py` | Must equal grow's `_API_VERSION` — the Pico↔Server REST contract generation reported in `GET /` and the announce payload (`api_version`). Bumped only on breaking contract changes. |
| **Mock's own service version** | `pyproject.toml` `version` (exposed via `importlib.metadata` as `mushpi_mock.__version__`) | Independent SemVer for the mock tool itself — surfaced **only** through `GET /version`, never on the mirrored Pico endpoints. |

`GET /version` answers all three at once, so a demo can prove exactly which firmware the mock is emulating without confusing it with the tool's own release line:

```json
{ "mock_version": "0.1.0", "represented_firmware_version": "0.8.4", "represented_api_version": 1 }
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
