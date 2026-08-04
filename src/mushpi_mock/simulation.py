"""
Hysteresis-based sensor simulation engine.

NestJS mapping:
    This is like a @Cron() service that periodically updates sensor values.
    In NestJS you'd use:
        @Cron(CronExpression.EVERY_5_SECONDS)
        handleCron() { this.simulateTick(); }
    Here we use asyncio.create_task() with asyncio.sleep() instead.

The simulation produces realistic bi-directional sawtooth oscillation around
setpoints, matching the real Pico's hysteresis control loop with symmetric
deadbands (v0.7.0+):

    Humidity (symmetric deadband ±6%):
        humidity <= target - 6       → humidifier ON, fan OFF → humidity rises
        humidity > target + 6        → fan ON, humidifier OFF → humidity drops
        target - 6 < humidity <= target + 6 → both OFF → wide natural drift

        The wide symmetric deadband lets humidity explore the full ±6% range
        around the setpoint before an actuator engages. Within the deadband,
        large random drifts (±1.2 per tick) produce visible oscillation on
        dashboards.

    Temperature (symmetric deadband ±2°C):
        temperature <= target - 2    → heater ON  → temp rises
        temperature > target + 2     → heater OFF → temp drifts down
        target - 2 < temp <= target + 2 → heater OFF → wide natural drift

        The wide symmetric deadband lets temperature explore the full ±2°C
        range around the setpoint. Within the deadband, large random drifts
        (±0.6 per tick) produce visible oscillation on dashboards.

    Cross-coupling (physical realism):
        Humidifier ON → evaporative cooling lowers temperature slightly.
        Heater ON → warm air drying lowers relative humidity slightly.

    When control_loop_enabled = false: all relays OFF, free random walk.

The core logic lives in `compute_hysteresis_tick()` — a pure function shared
by both the live SimulationEngine and the offline seed generator (seed.py).
This avoids duplicating the hysteresis rules in two places.
"""

from __future__ import annotations

import asyncio
import random

from .state import UnitState

# ── Simulation tuning constants ──────────────────────────────────
# Deadband widths: how far from target before correction engages
TEMP_DEADBAND = 2.0      # ±2°C around setpoint
HUM_DEADBAND = 6.0       # ±6% around setpoint

# Drift within deadband (per tick, additive)
TEMP_DRIFT_MIN = -0.6    # max negative drift per tick
TEMP_DRIFT_MAX = 0.6     # max positive drift per tick
HUM_DRIFT_MIN = -1.2     # max negative drift per tick
HUM_DRIFT_MAX = 1.2      # max positive drift per tick

# Correction step sizes (per tick, additive)
TEMP_HEAT_STEP_MIN = 0.2
TEMP_HEAT_STEP_MAX = 0.6
TEMP_COOL_STEP_MIN = -0.6
TEMP_COOL_STEP_MAX = -0.2

HUM_RISE_STEP_MIN = 0.3
HUM_RISE_STEP_MAX = 0.7
HUM_FALL_STEP_MIN = -0.8
HUM_FALL_STEP_MAX = -0.3

# Cross-coupling effects (per tick, additive)
EVAP_COOL_MIN = 0.05     # humidifier cools air
EVAP_COOL_MAX = 0.2
HEATER_DRY_MIN = 0.1     # heater dries air
HEATER_DRY_MAX = 0.3

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

    All tuning parameters (deadband widths, drift amplitudes, correction
    step sizes, cross-coupling effects) are defined as module-level
    constants at the top of this file. See the ``TEMP_DEADBAND``,
    ``HUM_DEADBAND``, ``TEMP_DRIFT_*``, ``HUM_DRIFT_*``, ``TEMP_HEAT_STEP_*``,
    ``TEMP_COOL_STEP_*``, ``HUM_RISE_STEP_*``, ``HUM_FALL_STEP_*``,
    ``EVAP_COOL_*``, and ``HEATER_DRY_*`` constants.

    The algorithm uses two independent symmetric deadband control loops:

    Humidity — symmetric deadband (±6%, see ``HUM_DEADBAND``):
        humidity <= target - 6       → humidifier ON, fan OFF (humidity rises)
        humidity > target + 6        → fan ON, humidifier OFF (humidity drops)
        target ± 6                   → both OFF (wide natural drift ±1.2)

    Temperature — symmetric deadband (±2°C, see ``TEMP_DEADBAND``):
        temperature <= target - 2    → heater ON  (temp rises toward setpoint)
        temperature > target + 2     → heater OFF (temp drifts down naturally)
        target ± 2                   → heater OFF (wide natural drift ±0.6)

    Cross-coupling (physical realism):
        Humidifier ON → evaporative cooling lowers temp slightly.
        Heater ON → warm air drying lowers humidity slightly.

    The humidifier and fan are mutually exclusive — engaging either one
    forces the other OFF as a safety measure (matching firmware behavior).

    Args:
        temp: Current temperature reading.
        humidity: Current humidity reading.
        outputs: Current relay states {"fan": bool, "humidifier": bool, "heater": bool}.
        setpoints: Target values {"temperature": int, "humidity": int}.
        control_enabled: Whether the control loop is active.
        hyst_temp: Temperature hysteresis width (degrees C) — unused in new
                   symmetric deadband algorithm but kept for API compatibility.
        hyst_hum: Humidity hysteresis width (percent) — unused in new
                  symmetric deadband algorithm but kept for API compatibility.

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
        # ── Control enabled: symmetric deadband hysteresis ───────────
        # Wide bi-directional deadbands let sensors swing both above and
        # below the setpoint, producing visible oscillation on dashboards.
        # Deadband widths and step sizes are module-level constants — see
        # the top of this file for all tuning parameters.

        # ── Humidity control (symmetric deadband) ────────────────────
        hum_set = setpoints["humidity"]
        if humidity <= hum_set - HUM_DEADBAND:
            new_outputs["humidifier"] = True
            new_outputs["fan"] = False
            humidity += random.uniform(HUM_RISE_STEP_MIN, HUM_RISE_STEP_MAX)
        elif humidity > hum_set + HUM_DEADBAND:
            new_outputs["humidifier"] = False
            new_outputs["fan"] = True
            humidity += random.uniform(HUM_FALL_STEP_MIN, HUM_FALL_STEP_MAX)
        else:
            # Wide drift within deadband = explores 74-86% range
            new_outputs["humidifier"] = False
            new_outputs["fan"] = False
            humidity += random.uniform(HUM_DRIFT_MIN, HUM_DRIFT_MAX)

        # ── Temperature control (symmetric deadband) ─────────────────
        temp_set = setpoints["temperature"]
        if temp <= temp_set - TEMP_DEADBAND:
            new_outputs["heater"] = True
            temp += random.uniform(TEMP_HEAT_STEP_MIN, TEMP_HEAT_STEP_MAX)
        elif temp > temp_set + TEMP_DEADBAND:
            new_outputs["heater"] = False
            temp += random.uniform(TEMP_COOL_STEP_MIN, TEMP_COOL_STEP_MAX)
        else:
            # Wide drift within deadband = explores 23-27°C range
            new_outputs["heater"] = False
            temp += random.uniform(TEMP_DRIFT_MIN, TEMP_DRIFT_MAX)

        # ── Cross-coupling (physical realism) ─────────────────────────
        if new_outputs["humidifier"]:
            temp -= random.uniform(EVAP_COOL_MIN, EVAP_COOL_MAX)   # evaporative cooling
        if new_outputs["heater"]:
            humidity -= random.uniform(HEATER_DRY_MIN, HEATER_DRY_MAX) # warm air drying

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
