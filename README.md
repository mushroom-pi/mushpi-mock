# mushpi-mock

Mock Pico growing units — mirrors the [`mushpi-grow`](../mushpi-grow/) firmware's REST API with synthetic sensor data. Use for development, demos, screenshots, and testing without real hardware.

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
5. **Serve**: The FastAPI app exposes all Pico endpoints — `GET /`, `/sensors`, `/setpoints`, `/outputs`, `/control`, `/setup`, `/health`, `/system`, plus `POST`/`PUT` mutations.
6. **Poll**: The server polls `GET /` every 60 seconds (same as with real Pico units) and stores readings in SQLite.

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

All endpoints mirror the real Pico firmware. See [`mushpi-grow/spec/openapi.yaml`](../mushpi-grow/spec/openapi.yaml) for the canonical API contract.

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

## Version Alignment

The mock's software version must match the real firmware's `_SOFTWARE_VERSION` in [`mushpi-grow/app/state.py`](../mushpi-grow/app/state.py). Run the version check to verify:

```bash
uv run python -c "
import json, re
version_match = re.search(r'_SOFTWARE_VERSION = \"([^\"]+)\"',
    open('../mushpi-grow/app/state.py').read())
grow_version = version_match.group(1)
mock_version = open('src/mushpi_mock/VERSION').read().strip()
assert grow_version == mock_version, f'Mismatch: mock={mock_version}, grow={grow_version}'
print(f'OK: mock={mock_version}, grow={grow_version}')
"
```

The `mushpi-mock` agent checks this alignment on every build. If you manually change the firmware version, also update `src/mushpi_mock/VERSION` and the `_SOFTWARE_VERSION` constant in `state.py`.

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
