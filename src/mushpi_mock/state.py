"""
In-memory state for a single mock Pico unit.

NestJS mapping:
    This is like a Service that holds mutable state. In NestJS you'd inject
    it via constructor DI. Here we pass it via FastAPI's Depends() or
    store it in app.state (Starlette's built-in per-app storage).

    Key difference from NestJS: Python doesn't have constructor injection,
    so we use plain classes and pass them around explicitly. FastAPI's
    Depends() system is the closest equivalent to NestJS DI.

Each UnitState instance holds everything a real Pico holds in its
module-level dicts (status, setpoints, devices) plus simulated uptime
and system info.
"""

from __future__ import annotations

import time
from typing import Any

# Must match mushpi-grow/app/state.py::_SOFTWARE_VERSION (the source of truth).
# Also keep src/mushpi_mock/VERSION in sync — it's a plain-text version file
# used for automated checks between the two repos.
_SOFTWARE_VERSION = "0.6.0"


class UnitState:
    """All in-memory state for one mock Pico unit.

    Mirrors the firmware's module-level dicts:
        - status    → self._sensors + self._outputs
        - setpoints → self.setpoints
        - devices   → self.devices
    Plus additional state for uptime, system info, and control loop.
    """

    def __init__(self, handle: str, port: int) -> None:
        self.handle = handle
        self.port = port
        self.boot_time = time.time()

        # ── Sensors (mirrors firmware's status["temperature"], status["humidity"]) ──
        self._sensors: dict[str, Any] = {
            "temperature": 24.0,
            "humidity": 80.0,
            "last_ok_age_s": 0,
            "last_error": None,
        }

        # ── Outputs (mirrors firmware's status["fan"], status["humidifier"], ...) ──
        self._outputs: dict[str, bool] = {
            "fan": False,
            "humidifier": False,
            "heater": False,
        }

        # ── Setpoints (mirrors firmware's setpoints dict) ──
        # Humidity setpoint=80 matches the simulation's 70-90 range so the
        # hysteresis sawtooth oscillates meaningfully out of the box.
        self.setpoints: dict[str, int] = {"temperature": 25, "humidity": 80}

        # ── Control loop config (mirrors firmware's control dict) ──
        # hyst_temp=1 and hyst_hum=5 match the firmware defaults in config_loader.py.
        self.control_config: dict[str, int] = {
            "period_s": 5,
            "hyst_temp": 1,
            "hyst_hum": 5,
        }

        # ── Devices / GPIO setup (mirrors firmware's devices dict) ──
        self.devices: dict[str, Any] = {
            "pins": {"dht": 4, "humidifier": 6, "fan": 7, "heater": 8},
            "active_high": False,
        }

        # ── Control loop ──
        self.control_enabled: bool = True

        # ── Uptime tracking ──
        self._cumulative_s: int = 0
        self._reboot_reason: str = "power_on"

        # ── System info (built once at init, like firmware's _system_info) ──
        self._system_info: dict[str, Any] = {
            "software": {"version": _SOFTWARE_VERSION, "device_name": handle},
            "micropython": {
                "name": "micropython",
                "version": "1.26.0",
                "version_tuple": (1, 26, 0),
                "mpy": None,
                "build": "v1.26.0 on 2026-01-01 (mock)",
            },
            "hardware": {
                "platform": "rp2",
                "board": "Raspberry Pi Pico W with RP2040 (mock)",
                "port": "rp2",
                "cpu": {"freq_mhz": 133},
            },
            "wifi": {
                "mac": "de:ad:be:ef:00:01",
                "ip": "127.0.0.1",
                "port": port,
                "hostname": handle,
            },
        }

    # ── Uptime ────────────────────────────────────────────────────────────

    def get_uptime_info(self) -> dict[str, Any]:
        """Return uptime dict matching the firmware's uptime.get_uptime_info().

        The firmware tracks total_s (cumulative across scheduled reboots)
        and since_boot_s (current session). We simulate both.
        """
        since_boot = int(time.time() - self.boot_time)
        return {
            "total_s": self._cumulative_s + since_boot,
            "since_boot_s": since_boot,
            "reboot_reason": self._reboot_reason,
            "scheduled_reboot": {
                "enabled": False,
                "hour": 4,
                "minute": 0,
            },
        }

    # ── Health snapshot ───────────────────────────────────────────────────

    def get_health_snapshot(self) -> dict[str, Any]:
        """Return health dict matching the firmware's system_snapshot().

        The firmware reports real RAM, filesystem, WiFi RSSI, and MCU temp.
        We return realistic-looking mock values.
        """
        return {
            "mem": {"total": 524288, "used": 262144, "free": 262144, "used_pct": 50},
            "fs": {"total": 2097152, "used": 524288, "free": 1572864, "used_pct": 25},
            "wifi": {"connected": True, "rssi": -45},
            "mcu_temp_c": 28.5,
            "event_loop_util_pct": 12,
            "uptime_s": int(time.time() - self.boot_time),
        }

    # ── Sensor reading ────────────────────────────────────────────────────

    def get_sensor_reading(self) -> dict[str, Any]:
        """Return sensor dict matching the firmware's sensor.get_latest_dht_read().

        The firmware returns:
            {sensor_ok, temperature, humidity, last_ok_age_s, last_error}
        """
        sensor_ok = (
            self._sensors["temperature"] is not None
            and self._sensors["humidity"] is not None
            and self._sensors["last_error"] is None
        )
        return {
            "sensor_ok": sensor_ok,
            "temperature": self._sensors["temperature"],
            "humidity": self._sensors["humidity"],
            "last_ok_age_s": self._sensors["last_ok_age_s"],
            "last_error": self._sensors["last_error"],
        }

    # ── Full snapshot (GET /) ─────────────────────────────────────────────

    def get_full_snapshot(self) -> dict[str, Any]:
        """Return the full system snapshot matching the firmware's GET / response.

        This is what the server's cron poller reads every 60 seconds.
        The shape must match exactly so ReadingsService.createFromDeviceResponse()
        can parse it.
        """
        return {
            "system": self._system_info,
            "health": self.get_health_snapshot(),
            "uptime": self.get_uptime_info(),
            "devices": self.devices,
            "sensors": {"dht": self.get_sensor_reading()},
            "outputs": dict(self._outputs),
            "setpoints": dict(self.setpoints),
            "control_loop_enabled": self.control_enabled,
            "control": dict(self.control_config),
        }

    # ── Simulation tick ───────────────────────────────────────────────────

    def simulate_tick(self) -> None:
        """Called by the simulation engine to age the sensor reading.

        The actual temperature/humidity update is done by the SimulationEngine
        (in simulation.py) which calls this to increment last_ok_age_s.
        """
        if self._sensors["last_ok_age_s"] is not None:
            self._sensors["last_ok_age_s"] += 1
