"""
FastAPI application factory + click CLI entry point.

NestJS mapping:
    This is like main.ts + app.module.ts combined. In NestJS:
        async function bootstrap() {
            const app = await NestFactory.create(AppModule);
            app.enableCors();
            await app.listen(5000);
        }
        bootstrap();

    Here we have:
        1. create_app() — builds a FastAPI app with CORS, router, and state
           (like NestFactory.create(AppModule))
        2. cli() — click CLI that starts one or more uvicorn servers
           (like running `nest start` but with multi-process support)

    Key difference: NestJS has a built-in module system for DI. FastAPI
    uses plain Python — we wire dependencies manually in create_app().

Startup sequence:
    1. HTTP server starts immediately (accepts requests right away)
    2. Background task: announce → seed → start simulation
    3. Simulation begins producing hysteresis-driven sensor data
"""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from typing import Any

import click
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .announce import announce_with_retry
from .config import MockConfig, UnitConfig, load_config
from .router import router
from .seed import seed_readings
from .simulation import SimulationEngine
from .state import _SOFTWARE_VERSION, UnitState


def create_app(
    unit_config: UnitConfig,
    server_url: str,
    hub_secret: str,
    seed_days: int = 0,
    db_path: str | None = None,
    announce_ip: str | None = None,
) -> FastAPI:
    """Create a FastAPI app for a single mock Pico unit.

    NestJS equivalent: NestFactory.create(AppModule) with providers
    configured in the module. Here we manually wire:
        - UnitState (the "service" holding mutable state)
        - SimulationEngine (the "cron job" updating sensors)
        - Announcement (the "bootstrap hook" that runs on startup)

    Args:
        unit_config: Handle and port for this unit.
        server_url: Server base URL for announce POST.
        hub_secret: Shared secret for X-Pico-Secret header.
        seed_days: If > 0, generate backdated readings on startup.
        db_path: Path to server's SQLite DB (required for seeding).
        announce_ip: IPv4 address announced as the unit's `ip`
            (falls back to 127.0.0.1 when None). Also mirrored in
            `GET /system` wifi info so both stay consistent.
    """
    state = UnitState(
        handle=unit_config.handle,
        port=unit_config.port,
        announce_ip=announce_ip,
        # Mirrors the firmware's `bool(cfg.get("hub_url"))`: the hub object on
        # GET / and the page's Hub card report whether a hub address exists at
        # all, separate from whether it has acked the latest announce.
        hub_configured=bool(server_url),
    )
    simulation = SimulationEngine(state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Startup/shutdown lifecycle.

        NestJS equivalent: onModuleInit() / onModuleDestroy() hooks.

        The HTTP server starts immediately (yield is reached right away).
        Announce + seeding + simulation start run in a background task so
        the server is responsive even if the hub is unreachable.

        Sequence:
            1. Server starts accepting requests (yield)
            2. Background task: announce → seed → start simulation
        """
        # ── Background init: announce → seed → start simulation ───────
        async def _init_and_start() -> None:
            """Announce, seed, then start simulation in the background.

            The server is already accepting requests — this task runs
            concurrently. Sensors show initial values until the simulation
            kicks in (which is fine for demos).
            """
            # 1. Announce (blocking within this task — retries with backoff)
            ok, unit_id = await announce_with_retry(
                state, server_url, hub_secret, announce_ip
            )

            # 2. Seed historical data
            if seed_days > 0:
                if not ok or unit_id is None:
                    print(
                        f"  [{unit_config.handle}] Skipping seed: announce failed "
                        f"(need pico_unit_id from server)"
                    )
                elif db_path is None:
                    print(
                        f"  [{unit_config.handle}] ERROR: --seed-days requires "
                        f"'db_path' in config.json or config file. "
                        f"Add: \"db_path\": \"../mushpi-server/data/app.sqlite\""
                    )
                else:
                    count = seed_readings(unit_id, db_path, seed_days, state)
                    if count > 0:
                        print(
                            f"  [{unit_config.handle}] Seeded {count} readings "
                            f"over {seed_days} day(s) → {db_path}"
                        )
                        print(
                            f"  [{unit_config.handle}] Post-seed sensors: "
                            f"temp={state._sensors['temperature']}°C, "
                            f"hum={state._sensors['humidity']}%"
                        )
                    # count == 0 means DB error — already logged by seed_readings()

            # 3. Start simulation (after announce + seed complete)
            simulation.start()
            print(
                f"  [{unit_config.handle}] Simulation started "
                f"(tick every {simulation.tick_interval}s)"
            )

        init_task = asyncio.create_task(_init_and_start())

        yield

        # ── Shutdown ──────────────────────────────────────────────────
        simulation.stop()
        init_task.cancel()
        print(f"  [{unit_config.handle}] Simulation stopped")

    app = FastAPI(
        title=f"MushPi Mock — {unit_config.handle}",
        # Deliberately the REPRESENTED firmware version: this app's OpenAPI
        # docs describe the mirrored Pico API, so info.version should read
        # like the firmware's. The mock's own service version is separate —
        # see GET /version (router.py) and mushpi_mock.__version__.
        version=_SOFTWARE_VERSION,
        lifespan=lifespan,
    )

    # CORS — match the firmware's CORS headers
    # NestJS equivalent: app.enableCors({ origin: '*' })
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register all routes
    app.include_router(router)

    # Store state and config on the app so routes can access them
    # NestJS equivalent: these would be injectable services
    app.state.unit_state = state
    app.state.simulation = simulation
    app.state.config = unit_config

    return app


# ── CLI ───────────────────────────────────────────────────────────────────


def _parse_handles(ctx: Any, param: Any, value: str | None) -> list[str] | None:
    """Click callback: split comma-separated handles into a list."""
    if value is None:
        return None
    handles = [h.strip() for h in value.split(",") if h.strip()]
    return handles if handles else None


@click.group()
def cli() -> None:
    """MushPi Mock — Mock Pico growing units for development and demos.

    Mirrors the mushpi-grow firmware's REST API with synthetic sensor data.
    Each mock unit runs as an independent FastAPI server on its own port.

    \b
    Examples:
        # Start a single mock unit on port 5000
        mushpi-mock start

        # Start 3 units on ports 5000-5002
        mushpi-mock start --count 3

        # Start with custom handles and ports
        mushpi-mock start --handles tent-a,tent-b --base-port 6000

        # Start from a config file
        mushpi-mock start --config config.json

        # Seed 14 days of historical data (requires db_path in config)
        mushpi-mock start --seed-days 14 --config config.json
    """
    pass


@cli.command()
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=False),
    default=None,
    help="Path to config.json file",
)
@click.option(
    "--count",
    "-n",
    type=int,
    default=1,
    help="Number of mock units to start (ignored if --config provides units)",
)
@click.option(
    "--handles",
    "-H",
    type=str,
    default=None,
    callback=_parse_handles,
    help="Comma-separated unit handles (e.g. mock-tent-1,mock-tent-2)",
)
@click.option(
    "--base-port",
    "-p",
    type=int,
    default=5000,
    help="Starting port number (units use base-port, base-port+1, ...)",
)
@click.option(
    "--seed-days",
    type=int,
    default=0,
    help="Generate N days of backdated readings on startup (requires --db-path or db_path in config)",
)
@click.option(
    "--db-path",
    "-d",
    type=str,
    default=None,
    help="Path to server's SQLite DB (required for --seed-days)",
)
@click.option(
    "--announce-ip",
    type=str,
    default=None,
    help=(
        "IPv4 address announced as this unit's `ip` (overrides config file). "
        "Must be a valid IPv4 — server rejects hostnames. "
        "Defaults to 127.0.0.1."
    ),
)
def start(
    config: str | None,
    count: int,
    handles: list[str] | None,
    base_port: int,
    seed_days: int,
    db_path: str | None,
    announce_ip: str | None,
) -> None:
    """Start one or more mock Pico units.

    Each unit runs as an independent FastAPI server on its own port,
    with its own sensor state and simulation engine. All units announce
    themselves to the server on startup.
    """
    # Load or build configuration
    if config:
        cfg = load_config(config)
        # CLI flag wins over the config-file value when provided
        if announce_ip is not None:
            cfg.announce_ip = announce_ip
    else:
        # Build config from CLI flags
        if handles is None:
            handles = [f"mock-tent-{i + 1}" for i in range(count)]

        units = [
            UnitConfig(handle=h, port=base_port + i)
            for i, h in enumerate(handles)
        ]
        cfg = MockConfig(units=units, db_path=db_path, announce_ip=announce_ip)

    print(f"Starting {len(cfg.units)} mock unit(s)...")
    for u in cfg.units:
        print(f"  {u.handle} → http://localhost:{u.port}")

    # Run all units concurrently
    asyncio.run(_run_all(cfg, seed_days))


async def _run_all(cfg: MockConfig, seed_days: int) -> None:
    """Start all mock units as concurrent uvicorn servers.

    NestJS equivalent: running multiple NestJS apps in parallel.
    We use asyncio.gather() to run all uvicorn servers concurrently.
    """
    servers: list[uvicorn.Server] = []

    for unit_cfg in cfg.units:
        app = create_app(
            unit_cfg,
            cfg.server_url,
            cfg.hub_secret,
            seed_days,
            cfg.db_path,
            cfg.announce_ip,
        )
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=unit_cfg.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        servers.append(server)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        print("\nShutting down all mock units...")
        stop_event.set()
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Start all servers concurrently
    # NestJS equivalent: Promise.all(servers.map(s => s.listen()))
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    await asyncio.gather(*tasks)


# Allow running as `python -m mushpi_mock`
if __name__ == "__main__":
    cli()
