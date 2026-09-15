# mushpi-mock — Reference (On-Demand)

Long-tail details and gotchas. **Load only when the task touches these areas** — do not read on every spawn. The always-loaded [`AGENTS.md`](./AGENTS.md) holds the overview, canonical references, project structure, version alignment, coding conventions, and workflow.

Topics covered here: simulation fidelity/hysteresis · seed database format · NestJS pattern mappings · teaching requirements.

---

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

### Intentional Omissions (v0.8.0+)

As of firmware v0.8.0, the real Pico 2W implements:
- **Asymmetric deadband**: separate ON and OFF thresholds via `humidity_deadband` and `temperature_deadband` config keys. The humidifier turns ON at `target − deadband` (not at target), creating a no-action buffer zone.
- **Minimum runtime**: a `min_runtime` gate (default 30s) that prevents a relay from turning OFF before it has been ON for the configured minimum duration, reducing actuator cycling.

The mock does **not** simulate these features. The simulation uses wide symmetric deadbands (±6% humidity, ±2°C temperature) that produce visually similar sawtooth patterns on dashboards. Implementing faithful asymmetric deadband + min-runtime simulation would add significant complexity with negligible benefit for demo purposes. The mock's primary function is REST API mirroring, not control dynamics replication.

If the mock's hysteresis simulation is ever rewritten to match a newer firmware version, these features should be considered for inclusion at that time.

### API Fidelity Rules

- **`GET /` response shape** must match the real Pico's 10-key snapshot, in this order: `system`, `firmware_version`, `api_version`, `health`, `uptime`, `devices`, `sensors`, `outputs`, `setpoints`, `control_loop_enabled`. `firmware_version` is the represented `_SOFTWARE_VERSION` string (mirrors `system.software.version`); `api_version` is the represented `_API_VERSION` integer. Do **not** include internal keys like `control` (config dict) in the full snapshot — those belong only on their dedicated endpoints.
- **Announce payload shape** must match `spec/openapi.yaml` → `components.schemas.AnnouncePayload` exactly — 11 keys, in the firmware's order: `handle`, `ip`, `mac`, `port`, `micropython_version`, `firmware_version`, `api_version`, `board`, `board_total_mem_byte`, `board_total_fs_byte`, `board_cpu_freq_mhz`. The pre-#21 `software_version` key is **gone**; the server's `ValidationPipe` has `forbidNonWhitelisted`, so sending it now fails the announce with 422.
- **`GET /version` is mock-only** — the firmware does not serve it; never add it to `spec/openapi.yaml` or the mirrored surface. It exists to keep the mock's dual version identities (own `mock_version` vs `represented_firmware_version`/`represented_api_version`) distinguishable (`mushpi-docs/versioning.md` §6).
- **`GET /control` response** must return only `{"enabled": bool}` — the real Pico does not expose config fields (`period_s`, `hyst_temp`, etc.) through this endpoint.
- **`POST /control`** must require the `enabled` key (not optional) and only accept `enabled` in the request body — the real Pico reads `period_s`/`hyst_*` from `config.json`, not from this endpoint.

## Seed Database Format — CRITICAL

The `seed_readings()` function writes directly to the server's SQLite `readings` table. The `ts` column is a TEXT column, and **the server's readings queries use TypeORM string ordering** (`{ ts: 'ASC' }` / `{ ts: 'DESC' }`), not datetime-aware comparison.

**The seed MUST produce timestamps in the same format as the server's cron poller**, otherwise readings sort incorrectly across the `T`-to-space boundary and the dashboard shows phantom gaps:

| Source | Format | Example |
|--------|--------|---------|
| Server cron | `YYYY-MM-DD HH:MM:SS` (space, no μs) | `2026-08-05 08:33:00` |
| Seed (must match) | `YYYY-MM-DD HH:MM:SS` (space, no μs) | `2026-08-05 10:18:43` |

**Never use `ts.isoformat()`** — it produces `2026-08-05T10:18:43.022726`. The `T` separator (ASCII 84) sorts differently from the space separator (ASCII 32), so space-separated and `T`-separated timestamps on the same date interleave incorrectly. The data is chronologically fine but string-sorted wrong — the dashboard shows an apparent multi-hour gap where none exists.

**Always use**: `ts.strftime('%Y-%m-%d %H:%M:%S')`

This is the single most common source of seed bugs and is hard to detect without checking the raw database format.

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

## Teaching Requirement

The project owner knows TypeScript + NestJS deeply but is learning Python. All code must include:
- Module docstrings explaining the file's role and NestJS equivalent
- Class/function docstrings explaining purpose
- Comments on non-obvious lines with TypeScript parallels when helpful
- A summary at the end of each build session covering what was done and key Python concepts used
