"""
Hysteresis-based sensor simulation engine.

NestJS mapping:
    This is like a @Cron() service that periodically updates sensor values.
    In NestJS you'd use:
        @Cron(CronExpression.EVERY_5_SECONDS)
        handleCron() { this.simulateTick(); }
    Here we use asyncio.create_task() with asyncio.sleep() instead.

The simulation produces realistic sawtooth oscillation around setpoints,
matching the real Pico's hysteresis control loop behavior:

    Humidity:
        below setpoint        → humidifier ON → humidity rises (+0.5 to +1.5/tick)
        above setpoint + hyst → fan ON        → humidity drops (-1.0 to -0.3/tick)
        in between            → no change      → drift ±0.3

    Temperature:
        below setpoint        → heater ON  → temp rises (+0.3 to +0.8/tick)
        above setpoint + hyst → heater OFF → temp drops (-0.5 to -0.1/tick)
        in between            → no change   → drift ±0.3

    When control_loop_enabled = false: all relays OFF, free random walk.

The core logic lives in `compute_hysteresis_tick()` — a pure function shared
by both the live SimulationEngine and the offline seed generator (seed.py).
This avoids duplicating the hysteresis rules in two places.
"""

from __future__ import annotations

import asyncio
import random

from .state import UnitState

# ── DHT11 physical limits ──────────────────────────────────────────────────
# The real sensor can't report outside these ranges. We clamp to prevent
# the simulation from drifting into unrealistic territory.
DHT11_TEMP_MIN = 0.0
DHT11_TEMP_MAX = 50.0
DHT11_HUM_MIN = 10.0
DHT11_HUM_MAX = 90.0


def compute_hysteresis_tick(
    temp: float,
    humidity: float,
    outputs: dict[str, bool],
    setpoints: dict[str, int],
    control_enabled: bool,
    hyst_temp: int = 1,
    hyst_hum: int = 5,
) -> tuple[float, float, dict[str, bool]]:
    """One tick of hysteresis simulation — the core algorithm.

    This is a **pure function** (no side effects, no state mutation) so it
    can be reused by both the live simulation engine and the seed generator.

    The three-zone hysteresis matches the real firmware's control_loop():
        Zone 1 (below setpoint):          actuator ON  → value moves toward setpoint
        Zone 2 (setpoint to setpoint+hyst): hold zone  → value drifts slightly
        Zone 3 (above setpoint+hyst):     actuator OFF → value moves away

    Args:
        temp: Current temperature reading.
        humidity: Current humidity reading.
        outputs: Current relay states {"fan": bool, "humidifier": bool, "heater": bool}.
        setpoints: Target values {"temperature": int, "humidity": int}.
        control_enabled: Whether the control loop is active.
        hyst_temp: Temperature hysteresis width (degrees C).
        hyst_hum: Humidity hysteresis width (percent).

    Returns:
        (new_temp, new_humidity, new_outputs) — all clamped to DHT11 ranges.
    """
    new_outputs = {
        "fan": outputs.get("fan", False),
        "humidifier": outputs.get("humidifier", False),
        "heater": outputs.get("heater", False),
    }

    if not control_enabled:
        # ── Control disabled: all relays OFF, free random walk ─────────
        # The real firmware calls relays_off() when control is disabled.
        # Sensors continue to be sampled but no actuators are driven.
        new_outputs["fan"] = False
        new_outputs["humidifier"] = False
        new_outputs["heater"] = False

        temp += random.uniform(-0.5, 0.5)
        humidity += random.uniform(-0.5, 0.5)
    else:
        # ── Humidity hysteresis ────────────────────────────────────────
        hum_set = setpoints["humidity"]
        if humidity < hum_set:
            # Zone 1: below setpoint → humidifier ON, humidity rises
            new_outputs["humidifier"] = True
            new_outputs["fan"] = False
            humidity += random.uniform(0.5, 1.5)
        elif humidity >= hum_set + hyst_hum:
            # Zone 3: above setpoint + hyst → fan ON, humidity drops
            new_outputs["humidifier"] = False
            new_outputs["fan"] = True
            humidity += random.uniform(-1.0, -0.3)
        else:
            # Zone 2: hysteresis band → no output change, small drift
            humidity += random.uniform(-0.3, 0.3)

        # ── Temperature hysteresis ─────────────────────────────────────
        temp_set = setpoints["temperature"]
        if temp < temp_set:
            # Zone 1: below setpoint → heater ON, temp rises
            new_outputs["heater"] = True
            temp += random.uniform(0.3, 0.8)
        elif temp >= temp_set + hyst_temp:
            # Zone 3: above setpoint + hyst → heater OFF, temp drifts down
            new_outputs["heater"] = False
            temp += random.uniform(-0.5, -0.1)
        else:
            # Zone 2: hysteresis band → no output change, small drift
            temp += random.uniform(-0.3, 0.3)

    # Clamp to DHT11 physical ranges
    temp = max(DHT11_TEMP_MIN, min(DHT11_TEMP_MAX, temp))
    humidity = max(DHT11_HUM_MIN, min(DHT11_HUM_MAX, humidity))

    return temp, humidity, new_outputs


class SimulationEngine:
    """Hysteresis-based sensor simulator for a single unit.

    Runs as an async background task, updating the unit's sensor values
    and relay states every `tick_interval` seconds. The hysteresis loop
    produces a characteristic sawtooth oscillation around setpoints —
    the same pattern the real Pico firmware generates.

    NestJS equivalent: a @Cron() service that calls control_loop() logic.
    """

    def __init__(
        self,
        state: UnitState,
        tick_interval: float = 2.0,
    ) -> None:
        self.state = state
        self.tick_interval = tick_interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def tick(self) -> None:
        """One simulation step: run hysteresis and update state.

        Reads current sensor values, setpoints, and control config from
        the unit state, runs the hysteresis algorithm, and writes back
        the new sensor values and relay states.
        """
        s = self.state

        new_temp, new_hum, new_outputs = compute_hysteresis_tick(
            temp=float(s._sensors["temperature"]),
            humidity=float(s._sensors["humidity"]),
            outputs=s._outputs,
            setpoints=s.setpoints,
            control_enabled=s.control_enabled,
            hyst_temp=s.control_config["hyst_temp"],
            hyst_hum=s.control_config["hyst_hum"],
        )

        # Write back — DHT11 returns integers, so round to match real hardware
        s._sensors["temperature"] = int(round(new_temp))
        s._sensors["humidity"] = int(round(new_hum))
        s._outputs.update(new_outputs)

        # Reset the "age" counter (sensor was just read)
        s._sensors["last_ok_age_s"] = 0
        s._sensors["last_error"] = None

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
