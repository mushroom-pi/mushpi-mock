# mushpi-mock — Reference (On-Demand)

Long-tail details and gotchas. **Load only when the task touches these areas** — do not read on every spawn. The always-loaded [`AGENTS.md`](./AGENTS.md) holds the overview, canonical references, project structure, version alignment, coding conventions, and workflow.

Topics covered here: simulation fidelity/hysteresis · runtime-state persistence · seed database format · NestJS pattern mappings · teaching requirements.

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
- When `control_loop_enabled = false`: relays turn off **once** via the engine's one-shot latch (`SimulationEngine.relays_latched`, mirroring the firmware loop's `relays_latched`) — then hand-set positions from `POST /outputs` survive while the loop stays off (no per-tick fight; a real unit does not wipe them). A boot restore that comes back with the loop **off** primes the latch, so restored positions survive the first disabled tick. Sensors drift freely (random walk)
- All values stay clamped within DHT11 range (temp 0–50°C, humidity 10–90%)

### Intentional Omissions (v0.8.0+)

As of firmware v0.8.0, the real Pico 2W implements:
- **Asymmetric deadband**: separate ON and OFF thresholds via `humidity_deadband` and `temperature_deadband` config keys. The humidifier turns ON at `target − deadband` (not at target), creating a no-action buffer zone.
- **Minimum runtime**: a `min_runtime` gate (default 30s) that prevents a relay from turning OFF before it has been ON for the configured minimum duration, reducing actuator cycling.

The mock does **not** simulate these features. The simulation uses wide symmetric deadbands (±6% humidity, ±2°C temperature) that produce visually similar sawtooth patterns on dashboards. Implementing faithful asymmetric deadband + min-runtime simulation would add significant complexity with negligible benefit for demo purposes. The mock's primary function is REST API mirroring, not control dynamics replication.

If the mock's hysteresis simulation is ever rewritten to match a newer firmware version, these features should be considered for inclusion at that time.

### API Fidelity Rules

- **`GET /` response shape** must match the real Pico's 11-key snapshot, in this order: `system`, `firmware_version`, `api_version`, `health`, `uptime`, `devices`, `sensors`, `outputs`, `setpoints`, `control_loop_enabled`, `hub`. `firmware_version` is the represented `_SOFTWARE_VERSION` string (mirrors `system.software.version`); `api_version` is the represented `_API_VERSION` integer; `hub` is the additive ack object and is always **last** (see the `hub` rule below). Do **not** include internal keys like `control` (config dict) in the full snapshot — those belong only on their dedicated endpoints.
- **Announce payload shape** must match `spec/openapi.yaml` → `components.schemas.AnnouncePayload` exactly — 11 keys, in the firmware's order: `handle`, `ip`, `mac`, `port`, `micropython_version`, `firmware_version`, `api_version`, `board`, `board_total_mem_byte`, `board_total_fs_byte`, `board_cpu_freq_mhz`. The former `software_version` key is **gone**; the server's `ValidationPipe` has `forbidNonWhitelisted`, so sending it now fails the announce with 422.
- **`GET /version` is mock-only** — the firmware does not serve it; never add it to `spec/openapi.yaml` or the mirrored surface. It exists to keep the mock's dual version identities (own `mock_version` vs `represented_firmware_version`/`represented_api_version`) distinguishable (normative rules: `mushpi-ops/MAINTAINING.md` §Version & Release Semantics).
- **`GET /control` response** must return only `{"enabled": bool}` — the real Pico does not expose config fields (`period_s`, `hyst_temp`, etc.) through this endpoint.
- **`POST /control`** must require the `enabled` key (not optional) and only accept `enabled` in the request body — the real Pico reads `period_s`/`hyst_*` from `config.json`, not from this endpoint.
- **Firmware-matching error bodies require `JSONResponse`, not `HTTPException`** — `HTTPException` emits `{"detail": ...}` while the firmware's POST handlers emit `{"ok": false, "error": ...}`; returning `JSONResponse(status_code=..., content={"ok": False, "error": ...})` preserves mock/device parity for the same endpoint (e.g. the `POST`/`PUT /setup` GPIO-conflict 400 in `update_setup`).
- **A handler annotated as returning `dict | Response` (or any `Response` subclass) needs `response_model=None` on the route decorator** (FastAPI 0.141) — route registration otherwise raises `FastAPIError`, whose own hint is that remedy. Both stacked `/setup` decorators carry it for this reason.
- **Fidelity gap CLOSED on purpose — `/setup` pins.** The old `PinsDto` (`ge=1`, `le=29`) made FastAPI answer `422 {"detail": ...}` for out-of-range pins while the firmware answered `400 {"ok": false, "error": ...}`; worse, GP23–25/GP29 fell *inside* the Pydantic bound and were never refused at all. Closing this was not incidental — the on-unit page cannot report a firmware-shaped refusal for a reserved pin that validation never reaches — so `update_setup` now parses the raw JSON body and classifies every value through `pins.gpio_reject_reason`. Do **not** reintroduce a Pydantic-validated pins DTO. (No other 422-vs-400 note applies; the remaining endpoints keep their own validation.)
- **`GET /ui`, `GET /provision` and the negotiated `GET /` are the mirrored non-JSON responses.** `/ui` and `/provision` are always `text/html`. `GET /`'s negotiation copies the firmware predicate **verbatim** and stays strict: HTML only when the `Accept` header contains `text/html` **and** does not contain `application/json`; everything else — including the server's 60 s poll (`application/json, text/plain, */*`) and the page's own `Accept: application/json` fetch — gets JSON. Do not "improve" it into the usual quality-value heuristic.
- **`GET /static/{filename}` is a closed allowlist** (`unit.js` → `application/javascript`, `favicon.svg` → `image/svg+xml`, `Cache-Control: max-age=3600`); unknown or absent → `404 {"ok": false, "error": "static asset missing"}`. Never build a filesystem path from the request string. (The `3600` against the firmware's deliberate `max_age=0` is a known **open gap** — alignment is pending an orchestrator decision; do not change either side unilaterally.)
- **`hub: {configured, connected}` is additive and always the last snapshot key.** `connected` means the hub **acknowledged the most recent announce** — never live reachability, never a hub-outage detector. Sole writer is `announce.py`, mutating the dict in place: `configured` from `server_url` at loop start, `True` on a 2xx ack, `False` after every failed attempt; the announce is skipped in AP mode.
- **AP view-only `403`s on the four control POSTs are mirrored as shape, not behaviour.** They check `state.mode == "ap"` **before** any mutation and return the firmware's exact `{"ok": false, "error": "controls are only available in STA mode - AP provisioning is view-only"}`; the mock presents as `"sta"`, so the guards stand present-but-unreachable (keeping them is an open orchestrator decision — do not unilaterally add a `mode` surface). The provisioning surface itself is mirrored in full: **`GET /provision`** serves the vendored `provision.html` as `text/html`, unmodified apart from the firmware's own `DEVNAME` → handle substitution — no seam, because a faithful refusal of the form's submit is the correct simulated-unit behaviour — and answers `404 {"ok": false, "error": "provisioning form missing"}` (firmware JSON shape) if the vendor is unreadable; **`POST /provision`** mirrors the firmware's STA refusal invariantly — `403 {"ok": false, "error": "provisioning is only available in AP mode"}` before any state change — the mock has no AP path, so mirror the firmware body exactly rather than inventing a mock-specific message. Deliberately **no request DTO** on it: Pydantic validation would pre-empt the guard with a 422 on a malformed body, while the firmware 403s every POST from a connected unit regardless of body.
- **`POST /outputs` applies only the keys present** (≥1 required; an empty body returns the firmware-shaped `422 {"ok": false, "error": "the body must include at least one of: ..."}`), and the response always reads all three relays back from state. The unit page's Devices card sends only the switches that changed, so an all-three-required rule would refuse every single-switch toggle.
- **`/setup` pins are validated in-handler from the RAW body, in firmware order**: `pins.gpio_reject_reason` per role (assignable set 1–22, 26, 27, 28; named refusals for GP0 and GP23–25/GP29; bool and non-int → "must be a whole number"), **then** the merged-view duplicate rule, **then** `active_high`. The mock has **no boot-time config validation**: the firmware's separate, stricter boot duplicate-GPIO rule is deliberately **not** mirrored and must not be folded into this one.
- **The vendored pages are byte-identical; every mock-only difference lives in the render seam.** Editing `src/mushpi_mock/static/*` breaks `scripts/check-unit-page-drift.sh` and the "same page" requirement. Each anchor in `unit_page.py` must occur **exactly once** — a firmware reword raises at import, which is intended. The provisioning form (`provision.html`) vendors through the same manifest and hash flow but carries **no seam** — the only substitution is the firmware's own `DEVNAME` injection. Refresh with `scripts/refresh-unit-page.sh`, then re-point the anchors against the new page.

## Runtime-State Persistence — device `runtime.json` equivalent

`runtime_state.py::RuntimeStateStore` mirrors the firmware's `app/runtime_state.py` (the grow v0.8.4 behaviour). The rule that matters when touching the mutation handlers or the simulation:

- **File schema** (versioned, `_VERSION = 1`): `{version, setpoints{temperature, humidity}, control_enabled, outputs{fan, humidifier}}`. **There is no heater key** — `save_outputs()` takes no heater parameter by design. A file with an unknown top-level `version` loads as empty (never misread).
- **Keying**: one file per unit handle under `state_path` (default `~/.mushpi-mock/`), named `<percent-encoded-handle>.json` — the encoding is injective, so `--handles a,b` sharing one directory never collide.
- **Writers**: exactly the three mutation handlers in `router.py` — `update_setpoints` (persists the full pair read back from state, not the partial body), `update_outputs` (persists the applied fan/humidifier pair), `update_control` (persists only the `enabled` flag; the disable-relays-off is in-memory, **not** a persistence event). `simulation.py` never imports `runtime_state`. A save failure logs on the unit and does **not** fail the request (firmware parity).
- **Atomic write**: `.tmp` + `os.replace` (CPython's equivalent of the firmware's `.tmp` + rename).
- **Restore** (`main.create_app`, once, before anything serves — the mock's boot slot): targets written **in place** into `state.setpoints` (the dict is shared with the snapshot builders; never reassign it), then the loop flag; **loop on → touch no outputs** (the loop drives from its first tick); **loop off → prime `relays_latched`, force heater OFF first and unconditionally, then apply stored fan/humidifier**. Missing/corrupt file → state.py defaults, never raises into startup.
- **Restore-time normalisation** (mirrors the firmware's `_load()`): setpoints must be `int` (a `bool` is corruption — Python's bool-is-int subclassing is why the check is explicit), clamped at restore to 0–50 °C / 10–90 % RH; `control_enabled`/outputs must be strict bools; individual corrupt fields are dropped so the good ones survive. The live `POST /setpoints` endpoint does **no** range check — clamping is restore-time only.
- **Hysteresis config (`period_s`, `hyst_*`, deadbands, `min_runtime`) and pin mapping (`POST /setup`) are not runtime state** — the device loads them from `config.json` at boot; the mock keeps them in memory and they die with the process. Do not add them to the state file.
- **Deliberate divergences from the device** (documented on the unit page's banner): the mock's durability is a file, not flash (deleting the state file resets the unit); the firmware's AP-boot restore branch is not mirrored (the mock has no AP mode).

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

## Packaging & the ops image contract

**This repo carries no packaging.** The demo mock image is built and published from `mushpi-ops`, which materializes `mushpi-mock/` as a **build input** — exactly as the hub image consumes `mushpi-server/` and `mushpi-client/`. There is deliberately no `Dockerfile`, no `.github/workflows/`, and no CI here. Before adding any of them, check `mushpi-ops/REFERENCE.md` § Demo stack & the mock image: the image already exists and this is the wrong place for it.

Two facts in `pyproject.toml` are a **consumed interface**, because `mushpi-ops/Dockerfile.mock` builds against them. Both are build-guarded, so a mistake fails CI rather than a running demo:

| Fact | Guard | What breaks if you change it |
|---|---|---|
| `[project.scripts] mushpi-mock = "mushpi_mock.main:cli"` | `RUN mushpi-mock --help` runs during the image build | Renaming the script — or breaking the import chain behind it — **fails the ops build** (exit 127 / import error). It does not silently ship. |
| `requires-python` | `uv sync --frozen` under `UV_PYTHON_PREFERENCE=only-system` | Raising the floor fails the ops build at install time, because the image's fixed interpreter no longer satisfies it. |

The Python floor is only guarded while that `only-system` pin holds; without it, uv would fetch a uv-managed interpreter, the build would pass on an interpreter the base image does not ship, and the guard would vanish silently. If you need a third borrowed fact guarded, add the assertion to `mushpi-ops/Dockerfile.mock` rather than trusting it.

Practical consequences for the mock agent: a version bump or a CLI change here may need a **corresponding ops rebuild** rather than an edit in this repo, and `mushpi-mock`'s own demo profile (five units, seeding, the drifting unit) is tracked as `mushpi-mock#3` on the project board — not in the ops stack.
