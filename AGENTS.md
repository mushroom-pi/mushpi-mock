# mushpi-mock — Agent Conventions

These conventions are read by the `mushpi-mock` agent. The agent is a **single agent** (no planner/builder split) — it plans and builds in one pass.

> **Reference**: on-demand details (simulation fidelity/hysteresis spec, seed database format, NestJS pattern mappings, teaching requirements) live in [`REFERENCE.md`](./REFERENCE.md). Load it **only when the task touches those areas** — do not read it on every spawn.

## Project Overview

`mushpi-mock` is a Python FastAPI service that mirrors the `mushpi-grow` firmware's REST API. It uses uv for package management, FastAPI for HTTP, Pydantic v2 for validation, click for CLI, and httpx for server communication. Each mock unit is an independent uvicorn server with synthetic sensor data.

## Canonical References

1. **Pico API contract**: `mushpi-grow/spec/openapi.yaml` — the mock must mirror every endpoint, request/response shape, and behavior defined here. The announce payload is documented in `components.schemas.AnnouncePayload` (it targets the *server's* URL, so it is deliberately not under `paths:`). The unit page and its assets are mirrored the same way — vendored byte-identically under `src/mushpi_mock/static/`, with the mock-only differences applied at render time in `unit_page.py`, never in the vendored bytes.
2. **Firmware version**: `mushpi-grow/app/state.py` → `_SOFTWARE_VERSION`, and **API contract version**: `_API_VERSION` — the mock's represented versions must always match. See Version Alignment below.
3. **Feature design**: issue **#1** in `mushroom-pi/mushpi-mock` — "Mock Pico Units", the migrated form of roadmap #18. The old `mushpi-docs/roadmap/features/18-mock-pico-units.md` was deleted once migrated; the issue body is now the spec, and its dated comment is the completion record.

## Project Structure

```
mushpi-mock/
├── pyproject.toml        # Dependencies, scripts, build config
├── uv.lock               # Lockfile (like yarn.lock)
├── config.example.json   # Example config with defaults
├── README.md             # User-facing documentation
├── AGENTS.md             # This file
├── LICENSE               # MIT (verbatim SPDX text)
├── .python-version       # Python version pin
├── scripts/
│   ├── check-version-alignment.sh  # grow ⇔ mock version check (run manually)
│   ├── check-unit-page-drift.sh    # vendored page assets vs grow (exit 0/1/2)
│   └── refresh-unit-page.sh        # re-vendor from ../mushpi-grow after a page change
├── .venv/                # Virtual environment (auto-created by uv)
└── src/mushpi_mock/
    ├── __init__.py        # Package docstring + `__version__` (mock's OWN version, from pyproject.toml metadata)
    ├── VERSION            # REPRESENTED firmware version (must match grow; NOT the mock's own version)
    ├── main.py            # FastAPI app factory + click CLI
    ├── config.py          # Pydantic config models
    ├── state.py           # In-memory unit state (`_SOFTWARE_VERSION` + `_API_VERSION` = represented versions)
    ├── router.py          # All Pico API endpoints + mock-only `GET /version`
    ├── unit_page.py       # Vendored page loader + mock-only render seam
    ├── pins.py            # Reserved-GPIO rules mirrored from grow
    ├── simulation.py      # Hysteresis control-loop sensor data generator
    ├── seed.py            # Direct SQLite bulk seeding for --seed-days
    ├── announce.py        # Server announce + retry logic; sole writer of `hub.connected`
    └── static/            # Byte-identical vendored page assets + VENDOR manifest
```

## Config Keys & CLI Flags

Top-level `MockConfig` keys (`config.py`, JSON via `--config`) and `start` CLI flags (`main.py`). CLI flags shown override the config-file value when provided.

| Config key | CLI flag | Default | Purpose |
|---|---|---|---|
| `server_url` | — | `http://localhost:3000` | Server base URL for the announce POST |
| `hub_secret` | — | `mushpi-dev-secret` | `X-Pico-Secret` header value on announce |
| `announce_ip` | `--announce-ip` | `127.0.0.1` when unset | IPv4 announced as the unit's `ip` (mirrored in `GET /system` → `wifi.ip`). Server validates it with `@IsIP('4')` — invalid values are rejected with 422 (global ValidationPipe: `forbidNonWhitelisted` + `UnprocessableEntityException` factory, so hostnames and unknown payload keys also fail). Top-level (shared by all units), not per-unit |
| `db_path` | `--db-path` / `-d` | `None` | Path to server's SQLite DB (required for `--seed-days`) |
| `units[]` (`handle`, `port`) | `--handles` / `-H`, `--count` / `-n`, `--base-port` / `-p` | one `mock-tent-1` on port 5000 | Units to run |
| — | `--seed-days` | `0` | Days of backdated readings to seed on startup |
| — | `--config` / `-c` | `None` | Path to config JSON |

## Version Alignment — MANDATORY (Dual-Versioning)

The mock carries **two version identities** (normative rules: `mushpi-ops/MAINTAINING.md` §Version & Release Semantics):

| Identity | Lives in | Must match | Where it surfaces |
|---|---|---|---|
| **Represented firmware version** | `src/mushpi_mock/VERSION` + `state.py::_SOFTWARE_VERSION` | `mushpi-grow/app/state.py::_SOFTWARE_VERSION` | `GET /system`, `GET /` (+ its `firmware_version` key), announce payload |
| **Represented API contract version** | `state.py::_API_VERSION` | `mushpi-grow/app/state.py::_API_VERSION` | `GET /` `api_version` key, announce payload `api_version` |
| **Mock's own service version** | `pyproject.toml` `version` (exposed as `mushpi_mock.__version__` via `importlib.metadata`) | nothing — independent SemVer | `GET /version` only (`mock_version` field) |

The mock and the firmware it represents **must always report the same version**. The version appears in `GET /system` and `GET /` responses, and the server uses it for display.

**Single sources of truth**: `mushpi-grow/app/state.py` → `_SOFTWARE_VERSION` + `_API_VERSION`

When the firmware version bumps, **two** mock places need updating:

1. `src/mushpi_mock/VERSION` — plain text file with just the represented firmware version string (e.g., `0.8.4`)
2. `src/mushpi_mock/state.py` → `_SOFTWARE_VERSION` constant

When `_API_VERSION` bumps in grow (a breaking Pico↔Server contract change), update `state.py::_API_VERSION` **in the same pass** — an out-of-date mock would silently mask a real incompatibility. The mock's own `pyproject.toml` version is bumped independently, whenever the mock tool itself changes meaningfully.

`GET /version` returns all three (`mock_version`, `represented_firmware_version`, `represented_api_version`) — it is a mock-only route, NOT part of the mirrored Pico API, and must not be added to `spec/openapi.yaml`.

### When to update

- After **any** `mushpi-grow` build that bumps `_SOFTWARE_VERSION` (or `_API_VERSION`)
- The orchestrator triggers `mushpi-mock` with the version bump details

### Verification command

```bash
./scripts/check-version-alignment.sh
```

Runs two checks: firmware (grow `_SOFTWARE_VERSION` vs **both** `src/mushpi_mock/VERSION`
and `src/mushpi_mock/state.py::_SOFTWARE_VERSION`) and API (grow `_API_VERSION` vs
`src/mushpi_mock/state.py::_API_VERSION`). Prints a pass/fail line per check.
Exit 0 = both aligned, 1 = any mismatch / extraction failure, 2 = grow checkout not
found. Read-only.

## Coding Conventions

- **Python 3.12+** syntax: `str | None`, not `Optional[str]`
- **uv** for all package operations: `uv add`, `uv sync`, `uv run`
- **Pydantic v2**: `model_validate()`, `@field_validator`, `model_config` — never v1 patterns
- **FastAPI lifespan** context manager (`@asynccontextmanager`), not `@app.on_event`
- **Type hints** on all function signatures
- **Docstrings** on every module, class, and non-trivial function
- **NestJS analogies** in module docstrings to help the TypeScript-savvy developer

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
6. Verify: run the version-alignment check, then start the mock and hit endpoints with curl/httpx (see **Verification** below)
7. Update `VERSION` file and `_SOFTWARE_VERSION` if needed
8. Report back to orchestrator with summary + verification results

### Verification — what this repo actually supports

There is **no** pytest/ruff/mypy test suite, linter, or type checker configured in this repo. `pyproject.toml` declares runtime dependencies only — no test/lint config, no CI. Do not treat them as gates: `uv run pytest` finds no tests, and `uvx ruff check src` / `uvx mypy src` run without config and surface pre-existing, unactionable findings (fine for an ad-hoc look, never a required gate).

The two real checks, run from `mushpi-mock/`:

1. **Version alignment** — run `./scripts/check-version-alignment.sh` (firmware three-way check: grow `_SOFTWARE_VERSION` vs `src/mushpi_mock/VERSION` and `src/mushpi_mock/state.py`; plus API two-way check: grow `_API_VERSION` vs `src/mushpi_mock/state.py`). Exit 0 = all aligned.
2. **API smoke test** — start the mock and exercise the endpoints it serves (`GET /sensors`, `PUT /setpoints`, `GET /`, `GET /ui`, `GET /static/unit.js`), per `README.md` § Verification. This is the functional gate for any API change.
