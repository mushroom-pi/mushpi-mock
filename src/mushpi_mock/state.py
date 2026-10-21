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

import hashlib
import time
from typing import Any

# Must match mushpi-grow/app/state.py::_SOFTWARE_VERSION (the source of truth).
# Also keep src/mushpi_mock/VERSION in sync — it's a plain-text version file
# used for automated checks between the two repos.
# This is the *represented firmware version* — the mock's own service version
# lives in pyproject.toml (exposed separately via GET /version).
_SOFTWARE_VERSION = "0.8.4"

# Must match mushpi-grow/app/state.py::_API_VERSION (source of truth).
# Pico↔Server REST contract generation: bumped only on a breaking contract
# change (endpoint removed/renamed, incompatible shapes, changed semantics).
# Additive changes do NOT bump it. Canonical docs: spec/openapi.yaml
# (AnnouncePayload + GET / schemas).
_API_VERSION = 1

# The board the mock simulates, as the identity the page/hub report.
# SINGLE SOURCE OF TRUTH: `_system_info["hardware"]["board"]` is the only writer
# of this value, and `GET /`, `GET /system` and the announce payload all read it
# from there — so the three can never disagree about which board is claimed.
# Requirement: identify as a Pico 2 W (the board represented), AND carry the
# simulated marker in the API-visible value, not only in the page's labels.
# The string stays well inside the server's announce `@Length(1, 255)` bound on
# the board field and is free text (no enum, no pattern) — verified against
# mushpi-server/src/modules/pico-units/pico-unit.dto.ts.
_BOARD_IDENTITY = "Raspberry Pi Pico 2 W with RP2350 (simulated)"

# The operating mode the unit presents. Mirrors the firmware's `make_app(mode=…)`
# argument, which drives the page bootstrap and the AP view-only guards.
# The mock ships as a STA (normal, hub-announcing) unit; there is no entry point
# that selects "ap" — the guards exist so the mirrored contract shape (AP control
# POSTs refuse with 403) is real code rather than an unimplemented promise.
_STA_MODE = "sta"


def _mac_for_handle(handle: str) -> str:
    """Derive this unit's Wi-Fi MAC address from its handle.

    A real Pico 2 W reports the burned-in interface address
    (``wlan.config('mac')`` formatted as ``":".join("%02x" % b …)`` in
    ``mushpi-grow/app/state.py``) — every board has a different one, and the
    hub stores it per unit, so a mock that gave all its units the same address
    both looks wrong and makes the units indistinguishable server-side.

    Scheme: ``02:`` + the first five bytes of ``sha256(handle)``.

    * ``02`` as the leading octet sets the locally-administered bit and clears
      the multicast bit — a software-assigned, unicast address. It also marks
      the value as not belonging to any physical NIC's OUI block, which is the
      honest thing for a simulated unit to report.
    * The remaining 5 octets come from the digest, so distinct handles yield
      distinct addresses (40 bits — collisions across a demo roster are
      negligible) and the same handle always yields the same address.

    Determinism across restarts is the point, hence ``hashlib.sha256`` and
    **not** Python's builtin ``hash()``: the builtin is salted per process
    (``PYTHONHASHSEED``), so it would hand the same unit a new identity on
    every boot and the hub would keep re-learning a fresh MAC.

    NestJS analogy: a pure factory function for an entity's identity column,
    like generating a deterministic id from a slug.
    """
    digest = hashlib.sha256(handle.encode("utf-8")).digest()
    return ":".join(f"{b:02x}" for b in (b"\x02" + digest[:5]))


class UnitState:
    """All in-memory state for one mock Pico unit.

    Mirrors the firmware's module-level dicts:
        - status    → self._sensors + self._outputs
        - setpoints → self.setpoints
        - devices   → self.devices
        - hub       → self.hub (single writer: announce.py; mutated in place)
    Plus additional state for uptime, system info, control loop, and the
    operating mode (`self.mode`) the page bootstraps in.

    Identity is per unit, not per process: the Wi-Fi MAC reported in
    `_system_info["wifi"]["mac"]` is derived from the handle (see
    `_mac_for_handle`), so N units in one `mushpi-mock start` look like N
    distinct boards to the hub — in the announce payload and in `GET /` alike.

    Two monotonic stamps (`last_poll_monotonic`, `last_announce_ok_monotonic`)
    feed announce.py's re-announce watchdog; they are mock-only state with no
    firmware counterpart.
    """

    def __init__(
        self,
        handle: str,
        port: int,
        announce_ip: str | None = None,
        hub_configured: bool = True,
    ) -> None:
        """Build the in-memory state for one unit.

        Args:
            handle: Unit handle (matches the server's PicoUnit handle).
            port: Port the mock unit listens on.
            announce_ip: IPv4 address to report in `_system_info["wifi"]["ip"]`,
                mirroring what announce.py sends as the unit's `ip`. Falls back
                to "127.0.0.1" when unset (the original hardcoded value).
            hub_configured: Whether this unit has a hub address at all. Mirrors
                the firmware's `bool(cfg.get("hub_url"))` — `hub.configured` on
                `GET /` and the page's Hub card derive from it. `main.py` passes
                `bool(server_url)`; defaults True because every mock unit
                announces to the hub by default.
        """
        self.handle = handle
        self.port = port
        self.boot_time = time.time()
        # Monotonic counterpart of boot_time, so elapsed-time maths (the poll
        # gap below) never mixes clocks: boot_time is wall-clock and can jump.
        self.boot_monotonic = time.monotonic()

        # ── Operating mode (firmware make_app parity) ──
        # The mock presents as a normal STA unit; no entry point selects "ap".
        self.mode: str = _STA_MODE

        # ── Hub-connection state (mirrors the firmware's module-level `hub`) ──
        # `connected` means the hub acknowledged the MOST RECENT announce — it is
        # NOT live hub reachability and cannot reflect an outage after the ack.
        # Single writer: announce.py (mutated in place, never reassigned).
        self.hub: dict[str, bool] = {"configured": bool(hub_configured), "connected": False}

        # ── Poll / announce bookkeeping (drives announce.py's watchdog) ──
        # `monotonic()` throughout — these are elapsed-time comparisons, and a
        # wall clock can jump (NTP, a laptop lid) and fake a 3-minute gap.
        #
        # `last_poll_monotonic`: when a `GET /` snapshot was last served, i.e.
        # the last time anybody asked the unit for its full state. Sole writer:
        # `mark_polled()`, called from the router's `GET /` handler.
        # `None` = never polled since this process started.
        self.last_poll_monotonic: float | None = None

        # `last_announce_ok_monotonic`: when the hub last acknowledged an
        # announce from this unit. Sole writer: announce.py, alongside its
        # existing single-writer claim on `hub["connected"]`. The watchdog needs
        # a latch that survives a later failed attempt — `hub["connected"]` is
        # cleared by every failure, so it cannot answer "has this unit ever
        # registered?". `None` = never announced successfully.
        self.last_announce_ok_monotonic: float | None = None

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
        # humidity_deadband, temperature_deadband, and min_runtime are v0.8.0+ keys
        # that the simulation does not use (see simulation.py module docstring).
        self.control_config: dict[str, int] = {
            "period_s": 5,
            "hyst_temp": 1,
            "hyst_hum": 5,
            "humidity_deadband": 3,      # v0.8.0+ — not used by simulation
            "temperature_deadband": 1,   # v0.8.0+ — not used by simulation
            "min_runtime": 30,           # v0.8.0+ — not used by simulation
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
                # Single source: _BOARD_IDENTITY above. GET / , GET /system and
                # the announce payload all read this one field, so they cannot
                # drift apart on which board is claimed (AC E identity rule).
                "board": _BOARD_IDENTITY,
                "port": "rp2",
                # 133 MHz is the pre-existing mock value, kept deliberately: the
                # board identity above moved to the RP2350, but HARDWARE.md (the
                # project's hardware source of truth) does not state a clock for
                # it, and a real unit reports machine.freq() MEASURED. Do not
                # invent a figure here — settling this belongs with the hardware
                # reference, not with the page mirror.
                "cpu": {"freq_mhz": 133},
            },
            "wifi": {
                # Unique per unit, stable across restarts (see _mac_for_handle):
                # the announce payload and `GET /`/`GET /system` all read THIS
                # one field, so the identity the hub stores and the identity it
                # polls back can never disagree.
                "mac": _mac_for_handle(handle),
                # Mirrors the `ip` sent on announce (see announce.py).
                # Defaults to 127.0.0.1 when no announce_ip is configured.
                "ip": announce_ip or "127.0.0.1",
                "port": port,
                "hostname": handle,
            },
        }

    # ── Uptime ────────────────────────────────────────────────────────────

    def mark_polled(self) -> None:
        """Record that a full-snapshot `GET /` was just served (the poll beat).

        The server's cron reads `GET /?force=1` every 60 s; that request is the
        only proof the hub still knows this unit exists. announce.py's watchdog
        compares this stamp against its idle window, so the mark belongs on the
        snapshot route alone — never on the control endpoints, where a grower
        driving relays by hand would look like a healthy hub poll.

        Mock-only: the firmware tracks no poll history (the hub's poll is just
        an inbound request Microdot answers), and the real device re-announces
        only on boot. See announce.py § Poll-aware re-announce watchdog.
        """
        self.last_poll_monotonic = time.monotonic()

    def poll_gap_s(self) -> float:
        """Seconds since the last full-snapshot poll, or since boot-announce.

        Used for the watchdog's log line. When this process has never been
        polled, the reference is the last acknowledged announce — the unit is
        dark from the hub's point of view either way, so the longer that has
        gone on, the more reason the watchdog has to speak up.
        """
        reference = self.last_poll_monotonic
        if reference is None:
            reference = self.last_announce_ok_monotonic
        if reference is None:
            reference = self.boot_monotonic
        return max(0.0, time.monotonic() - reference)

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
        can parse it. Key order mirrors the firmware (app/api.py GET /):
        version metadata (`firmware_version`, `api_version`) sits immediately
        after `system`, before `health`; the additive `hub` object is last, after
        `control_loop_enabled`.
        """
        return {
            "system": self._system_info,
            # Version metadata (roadmap #21): mirrors the firmware's
            # firmware_version (SemVer string) / api_version (contract int) pair.
            "firmware_version": _SOFTWARE_VERSION,
            "api_version": _API_VERSION,
            "health": self.get_health_snapshot(),
            "uptime": self.get_uptime_info(),
            "devices": self.devices,
            "sensors": {"dht": self.get_sensor_reading()},
            "outputs": dict(self._outputs),
            "setpoints": dict(self.setpoints),
            "control_loop_enabled": self.control_enabled,
            # Hub-connection state (additive): {configured, connected}.
            # `connected` is an acknowledgement of the MOST RECENT announce,
            # written only by announce.py — never a live hub ping, so it cannot
            # reflect a hub outage after the ack. Mirrors the firmware's `hub`
            # object on GET /.
            "hub": dict(self.hub),
        }

    # ── Simulation tick ───────────────────────────────────────────────────

    def simulate_tick(self) -> None:
        """Called by the simulation engine to age the sensor reading.

        The actual temperature/humidity update is done by the SimulationEngine
        (in simulation.py) which calls this to increment last_ok_age_s.
        """
        if self._sensors["last_ok_age_s"] is not None:
            self._sensors["last_ok_age_s"] += 1
