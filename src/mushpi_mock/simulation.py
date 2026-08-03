"""
Random-walk sensor data generator.

NestJS mapping:
    This is like a @Cron() service that periodically updates sensor values.
    In NestJS you'd use:
        @Cron(CronExpression.EVERY_5_SECONDS)
        handleCron() { this.temperature += randomWalk(); }
    Here we use asyncio.create_task() with asyncio.sleep() instead.

The simulation produces realistic but not perfectly stable readings:
    - Temperature: 20-28 degrees C, +/-0.5 per tick
    - Humidity: 70-90%, +/-1 per tick
Both are clamped to realistic DHT11 ranges.
"""

from __future__ import annotations

import asyncio
import random

from .state import UnitState


class SimulationEngine:
    """Random-walk sensor simulator for a single unit.

    Runs as an async background task, updating the unit's sensor values
    every `tick_interval` seconds. The random walk is bounded so readings
    stay in a realistic range for mushroom cultivation.
    """

    def __init__(
        self,
        state: UnitState,
        tick_interval: float = 2.0,
        temp_min: float = 20.0,
        temp_max: float = 28.0,
        hum_min: float = 70.0,
        hum_max: float = 90.0,
    ) -> None:
        self.state = state
        self.tick_interval = tick_interval
        self.temp_min = temp_min
        self.temp_max = temp_max
        self.hum_min = hum_min
        self.hum_max = hum_max
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def tick(self) -> None:
        """One simulation step: random-walk temperature and humidity.

        The walk is symmetric (equal chance of going up or down) with
        configurable step sizes. Values are clamped to stay within
        realistic bounds.
        """
        # Temperature: +/- 0.5 degrees per tick
        # DHT11 returns integers — round to match real hardware behavior.
        temp = self.state._sensors["temperature"]
        if temp is not None:
            temp += random.uniform(-0.5, 0.5)
            temp = max(self.temp_min, min(self.temp_max, temp))
            self.state._sensors["temperature"] = int(round(temp))

        # Humidity: +/- 1% per tick
        # DHT11 returns integers — server DTO requires @IsInt() on humidity.
        hum = self.state._sensors["humidity"]
        if hum is not None:
            hum += random.uniform(-1.0, 1.0)
            hum = max(self.hum_min, min(self.hum_max, hum))
            self.state._sensors["humidity"] = int(round(hum))

        # Reset the "age" counter (sensor was just read)
        self.state._sensors["last_ok_age_s"] = 0
        self.state._sensors["last_error"] = None

    async def run(self) -> None:
        """Main loop: tick every `tick_interval` seconds until stopped.

        NestJS equivalent: @Cron() decorator on a handler method.
        Here we use a simple while loop with asyncio.sleep().
        """
        self._running = True
        while self._running:
            await self.tick()
            await asyncio.sleep(self.tick_interval)

    def start(self) -> None:
        """Start the simulation as a background asyncio task.

        NestJS equivalent: the @Cron() decorator auto-starts the job
        when the module initializes. Here we manually create the task.
        """
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        """Stop the simulation background task.

        NestJS equivalent: @Cron() jobs are auto-cancelled on module
        destruction. Here we cancel the task explicitly.
        """
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None


def seed_readings(state: UnitState, days: int) -> list[dict]:
    """Generate backdated synthetic readings for historical data.

    This fast-forwards the random walk simulation to produce `days` worth
    of readings at 60-second intervals. The readings are returned as a list
    of dicts (not POSTed to the server since there's no direct readings
    endpoint — the server only ingests readings via cron polling GET /).

    Useful for demos: run with --seed-days 14 to show 2 weeks of data.

    Returns:
        List of {"timestamp": str, "temperature": float, "humidity": float}
    """
    import datetime

    readings: list[dict] = []
    temp = 24.0
    hum = 80.0
    now = datetime.datetime.now()  # noqa: DTZ005
    start = now - datetime.timedelta(days=days)
    total_seconds = days * 86400

    for offset in range(0, total_seconds, 60):
        # Random walk (same bounds as the live simulation)
        temp += random.uniform(-0.5, 0.5)
        temp = max(20.0, min(28.0, temp))
        hum += random.uniform(-1.0, 1.0)
        hum = max(70.0, min(90.0, hum))

        ts = start + datetime.timedelta(seconds=offset)
        readings.append({
            "timestamp": ts.isoformat(),
            "temperature": int(round(temp)),
            "humidity": int(round(hum)),
        })

    # Update the live state to the final values so the simulation
    # continues from where the seed left off
    state._sensors["temperature"] = int(round(temp))
    state._sensors["humidity"] = int(round(hum))

    return readings
