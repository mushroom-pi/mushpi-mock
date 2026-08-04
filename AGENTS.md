# mushpi-mock — Agent Conventions

These conventions are read by the `mushpi-mock` agent. The agent is a **single agent** (no planner/builder split) — it plans and builds in one pass.

## Project Overview

`mushpi-mock` is a Python FastAPI service that mirrors the `mushpi-grow` firmware's REST API. It uses uv for package management, FastAPI for HTTP, Pydantic v2 for validation, click for CLI, and httpx for server communication. Each mock unit is an independent uvicorn server with synthetic sensor data.

## Canonical References

1. **Pico API contract**: `mushpi-grow/spec/openapi.yaml` — the mock must mirror every endpoint, request/response shape, and behavior defined here.
2. **Firmware version**: `mushpi-grow/app/state.py` → `_SOFTWARE_VERSION` — the mock's version must always match. See Version Alignment below.
3. **Feature design**: Roadmap #18 in `mushpi-docs/roadmap/mvp-roadmap.md`.

## Project Structure

```
mushpi-mock/
├── pyproject.toml        # Dependencies, scripts, build config
├── uv.lock               # Lockfile (like yarn.lock)
├── config.example.json   # Example config with defaults
├── README.md             # User-facing documentation
├── AGENTS.md             # This file
├── .python-version       # Python version pin
├── .venv/                # Virtual environment (auto-created by uv)
└── src/mushpi_mock/
    ├── __init__.py        # Package docstring
    ├── VERSION            # Single source of truth for mock version (must match grow)
    ├── main.py            # FastAPI app factory + click CLI
    ├── config.py          # Pydantic config models
    ├── state.py           # In-memory unit state
    ├── router.py          # All Pico API endpoints
    ├── simulation.py      # Random-walk sensor data generator
    └── announce.py        # Server announce + retry logic
```

## Version Alignment — MANDATORY

The mock and the firmware **must always report the same version**. The version appears in `GET /system` and `GET /` responses, and the server uses it for display.

**Single source of truth**: `mushpi-grow/app/state.py` → `_SOFTWARE_VERSION`

The mock has **two** places that need updating when the firmware version bumps:

1. `src/mushpi_mock/VERSION` — plain text file with just the version string (e.g., `0.6.0`)
2. `src/mushpi_mock/state.py` → `_SOFTWARE_VERSION` constant

Both must match `mushpi-grow/app/state.py::_SOFTWARE_VERSION`.

### When to update

- After **any** `mushpi-grow` build that bumps `_SOFTWARE_VERSION`
- The orchestrator triggers `mushpi-mock` with the version bump details

### Verification command

```bash
grow_ver=$(grep -oP '_SOFTWARE_VERSION\s*=\s*"\K[^"]+' ../mushpi-grow/app/state.py)
mock_ver=$(cat src/mushpi_mock/VERSION)
if [ "$grow_ver" != "$mock_ver" ]; then
    echo "VERSION MISMATCH: mock=$mock_ver, grow=$grow_ver"
    exit 1
fi
echo "Versions aligned: $mock_ver"
```

## Coding Conventions

- **Python 3.12+** syntax: `str | None`, not `Optional[str]`
- **uv** for all package operations: `uv add`, `uv sync`, `uv run`
- **Pydantic v2**: `model_validate()`, `@field_validator`, `model_config` — never v1 patterns
- **FastAPI lifespan** context manager (`@asynccontextmanager`), not `@app.on_event`
- **Type hints** on all function signatures
- **Docstrings** on every module, class, and non-trivial function
- **NestJS analogies** in module docstrings to help the TypeScript-savvy developer

## Simulation Fidelity — CRITICAL

The mock must produce **business-valuable data**, not noise. A real Pico running the hysteresis control loop produces a characteristic sawtooth oscillation around setpoints:

- **Humidity**: drops naturally over time → when below setpoint, humidifier ON → humidity rises → when above setpoint + hyst, fan ON, humidifier OFF → humidity drops. Repeat.
- **Temperature**: drops naturally → below setpoint, heater ON → rises → above setpoint + hyst, heater OFF. Repeat.

**The mock must simulate this hysteresis loop**, both in the live `SimulationEngine.tick()` and in the `seed_readings()` backdated data generator. A random walk without hysteresis produces flat noise that is useless for dashboards, charts, and demos — the frontend will show no meaningful patterns.

### Hysteresis simulation rules

- Setpoints define the targets (e.g., 25°C, 80% RH)
- `hyst_hum` = 5 (from firmware default), `hyst_temp` = 1
- **Humidity (asymmetric deadband above target)**:
  - When humidity ≤ setpoint: `humidifier_on = true`, `fan_on = false`, humidity trends **up** (+0.4 to +1.1 per tick). The humidifier chases the target aggressively — no lower hysteresis boundary.
  - When humidity > setpoint + hyst_hum: `humidifier_on = false`, `fan_on = true`, humidity trends **down** (-1.0 to -0.5 per tick). The fan only engages once humidity overshoots by the full hysteresis width.
  - When setpoint < humidity ≤ setpoint + hyst_hum: both OFF — natural deadband, humidity drifts ±0.3.
  - Humidifier and fan are mutually exclusive (safety off-calls when engaging either).
- **Temperature (bang-bang, no hysteresis band)**:
  - When temperature ≤ setpoint: `heater_on = true`, temp trends **up** (+0.3 to +0.8 per tick).
  - When temperature > setpoint: `heater_on = false`, temp trends **down** (-0.5 to -0.1 per tick).
  - Thermal lag in the drift rates produces natural oscillation around the setpoint.
- When `control_loop_enabled = false`: all relays OFF, sensors drift freely (random walk)
- All values stay clamped within DHT11 range (temp 0–50°C, humidity 10–90%)

## NestJS Pattern Mappings

| NestJS | mushpi-mock equivalent |
|--------|------------------------|
| `@Controller()` | `APIRouter` |
| `@Get()` `@Post()` | `@router.get()` `@router.post()` |
| DTO + `class-validator` | Pydantic `BaseModel` |
| `@Injectable()` service | Plain class stored in `app.state` |
| `ConfigModule` | `config.py` with Pydantic |
| `@Cron()` | `asyncio.create_task()` with `asyncio.sleep()` |
| `@OnModuleInit()` | FastAPI `lifespan` startup |
| `ValidationPipe` | Automatic Pydantic validation |
| `HttpService` (axios) | `httpx.AsyncClient` |

## When the Agent Runs

The `mushpi-mock` agent is triggered:

1. **Initial build**: Creating the project from scratch (what happened here)
2. **Pico API sync**: After any `mushpi-grow` change that affects the REST API surface (new/removed/changed endpoints, changed response shapes)
3. **Version sync**: After any `mushpi-grow` version bump
4. **Direct request**: When asked to update the mock directly

### Agent workflow

1. Read `mushpi-grow/spec/openapi.yaml` for the current API contract
2. Read `mushpi-grow/app/state.py` for the current firmware version
3. Compare with existing mock code (skip if fresh build)
4. Plan changes (identify files to touch, endpoints to add/update)
5. Build/update the mock code
6. Verify: start the mock, hit endpoints with curl/httpx
7. Update `VERSION` file and `_SOFTWARE_VERSION` if needed
8. Report back to orchestrator with summary + verification results

## Teaching Requirement

The project owner knows TypeScript + NestJS deeply but is learning Python. All code must include:
- Module docstrings explaining the file's role and NestJS equivalent
- Class/function docstrings explaining purpose
- Comments on non-obvious lines with TypeScript parallels when helpful
- A summary at the end of each build session covering what was done and key Python concepts used
