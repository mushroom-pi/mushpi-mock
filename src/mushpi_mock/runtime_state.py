"""
runtime_state.py — persistence of grower-chosen control state across a mock stop/start.

NestJS mapping:
    Like a small ``@Injectable()`` file-backed repository injected into the one
    controller that owns the three mutation handlers. In NestJS you'd write
    ``constructor(private runtime: RuntimeStateStore)`` in the PicoController;
    here FastAPI keeps the store on ``app.state`` (set in ``main.create_app``)
    and ``router.py`` reads it per request.

Holds the three things a grower sets by hand that must survive a restart —
mirrored from the firmware's ``app/runtime_state.py`` (mushpi-grow#17 behaviour):
the targets (setpoints), the control-loop on/off flag, and — when the loop is
off — the hand-set fan/humidifier positions. Stored as one JSON file per unit
handle under the ``state_path`` directory (default ``~/.mushpi-mock/``), written
atomically (``.tmp`` + ``os.replace``) like the firmware's ``.tmp`` + rename
pattern.

The heater is never persisted and never restored to ON: the schema has no
heater key, save_outputs() takes no heater parameter, and restore() forces
heater off unconditionally before applying anything else. Same safety rule as
the device.

Durability is for grower intent only. The simulation must never write here —
the firmware rule "control.py, hw.py and sensor.py must never import
runtime_state" maps structurally onto this repo: **simulation.py must never
import or call this module**. Only the three mutation handlers in ``router.py``
save, and only ``main.create_app`` calls ``restore()`` (the mock's boot slot).

Divergence from the device, deliberately not mirrored (the mock has no flash):
- the firmware's AP-mode boot branch (targets/flag only, outputs untouched) —
  the mock presents as STA with no AP entry point;
- ``config.json`` semantics for ``POST /setup`` — pin mapping stays memory-only
  here and is lost when the mock stops (disclosed on the unit page).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from .simulation import SimulationEngine
    from .state import UnitState

# Schema version — mirrors the firmware's _VERSION. A file written by an
# unknown future schema is ignored (loaded as empty), never misread.
_VERSION = 1

# Restore-time clamp bounds — the unit page's slider ranges (mirrors the
# firmware's runtime_state.py). Defensive clamp at RESTORE time only; the live
# POST /setpoints endpoint does no range check.
_TEMP_MIN = 0
_TEMP_MAX = 50
_HUM_MIN = 10
_HUM_MAX = 90

# Default home for the per-unit state files when `state_path` is unset.
# A fixed location under the user's home keeps the state independent of the
# CWD the mock happens to be started from (the device analogue: flash root is
# wherever the device is, not where you invoke it from).
DEFAULT_STATE_DIR = Path.home() / ".mushpi-mock"


def _clamped_int(val: Any, lo: int, hi: int) -> int | None:
    """Return val clamped into [lo, hi] as an int, or None if not a usable int.

    Mirrors the firmware: bool is a subclass of int, so a True/False setpoint
    is corruption, not a value — reject it explicitly. A float is likewise
    rejected: the contract for setpoints is integer degrees/percent."""
    if isinstance(val, bool) or not isinstance(val, int):
        return None
    if val < lo:
        return lo
    if val > hi:
        return hi
    return val


def _as_bool(val: Any) -> bool | None:
    """Return val if it is a strict bool, else None (mirrors the firmware)."""
    return val if isinstance(val, bool) else None


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop corrupt fields so the good ones survive, like the firmware's _load().

    Individual bad fields are removed; restore() then falls back to the
    state.py defaults for anything absent. Keys this schema does not know
    (e.g. a hand-added outputs.heater) are dropped here and never re-persisted.
    """
    out: dict[str, Any] = {}

    sp = raw.get("setpoints")
    if isinstance(sp, dict):
        good: dict[str, int] = {}
        t = _clamped_int(sp.get("temperature"), _TEMP_MIN, _TEMP_MAX)
        if t is not None:
            good["temperature"] = t
        h = _clamped_int(sp.get("humidity"), _HUM_MIN, _HUM_MAX)
        if h is not None:
            good["humidity"] = h
        if good:
            out["setpoints"] = good

    ce = _as_bool(raw.get("control_enabled"))
    if ce is not None:
        out["control_enabled"] = ce

    outs = raw.get("outputs")
    if isinstance(outs, dict):
        good_bools: dict[str, bool] = {}
        fan = _as_bool(outs.get("fan"))
        if fan is not None:
            good_bools["fan"] = fan
        hum = _as_bool(outs.get("humidifier"))
        if hum is not None:
            good_bools["humidifier"] = hum
        if good_bools:
            out["outputs"] = good_bools

    return out


class RuntimeStateStore:
    """One unit's runtime-state file: load, field-wise save, boot restore.

    The file is keyed per unit handle (``<state_dir>/<handle>.json``) so a
    multi-unit run (``--handles a,b -n 2``) never collides on one file. The
    handle is percent-encoded for filesystem safety; the encoding is injective,
    so distinct handles cannot map to the same path.

    NestJS equivalent: a file-backed repository bound to one unit id.
    """

    def __init__(self, handle: str, state_dir: str | os.PathLike[str] | None = None) -> None:
        """Bind the store to one unit handle, under `state_dir` (or the default).

        Args:
            handle: Unit handle — the persistence key (mirrors how the device's
                single runtime.json is keyed by the single device).
            state_dir: Directory for state files. None → DEFAULT_STATE_DIR.
                The path is expanduser()'d, so "~/..." works in config files.
        """
        self.handle = handle
        base = Path(state_dir).expanduser() if state_dir is not None else DEFAULT_STATE_DIR
        self.path = base / f"{quote(handle, safe='')}.json"

    # ── read ───────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        """Read the state file and return a normalised dict. Never raises.

        A missing, damaged, or unreadable file (first start, corrupted JSON)
        yields an empty dict; restore() then falls back to the state.py
        defaults. A top-level version mismatch yields an empty dict, so an
        unknown future schema is ignored rather than misread.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return {}
        return _normalise(raw)

    # ── write ──────────────────────────────────────────────────────────────

    def _atomic_write(self, data: dict[str, Any]) -> None:
        """Atomically write the state file via .tmp + os.replace.

        The CPython equivalent of the firmware's .tmp + os.rename pattern
        (os.replace is the portable atomic-overwrite rename). Raises on write
        failure; the callers in router.py catch and log — a state-write fault
        must never fail the request (firmware parity).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
        os.replace(tmp, self.path)

    def _write_field(self, key: str, value: Any) -> None:
        """load → replace one top-level key → atomic write.

        The shared body of the three save_* writers below (mirrors the
        firmware's _write_field). Raises on write failure; callers log.
        """
        data = self._load()
        data["version"] = _VERSION
        data[key] = value
        self._atomic_write(data)

    def save_setpoints(self, temperature: int, humidity: int) -> None:
        """Persist grower-chosen targets (router: POST/PUT /setpoints). Raises on write failure."""
        self._write_field("setpoints", {"temperature": int(temperature), "humidity": int(humidity)})

    def save_control_enabled(self, enabled: bool) -> None:
        """Persist the control-loop on/off flag (router: POST/PUT /control). Raises on write failure."""
        self._write_field("control_enabled", bool(enabled))

    def save_outputs(self, fan: bool, humidifier: bool) -> None:
        """Persist hand-set fan/humidifier positions (router: POST/PUT /outputs). Raises on write failure.

        There is no heater parameter by design: the heater is never restored
        to ON across a restart — the same safety rule as the device.
        """
        self._write_field("outputs", {"fan": bool(fan), "humidifier": bool(humidifier)})

    # ── boot restore ───────────────────────────────────────────────────────

    def restore(self, state: "UnitState", simulation: "SimulationEngine") -> None:
        """Single boot rehydrate entry point — called once from ``main.create_app``.

        Mirrors the firmware's ``runtime_state.restore()`` (minus its AP-mode
        branch — the mock has no AP boot) with the same three rules and the
        same order:

          1. targets are written IN PLACE into ``state.setpoints`` — the dict
             is shared with the snapshot builders and must never be reassigned;
          2. the loop flag is set, and the engine's ``relays_latched`` is
             primed together when the loop comes back off — without a primed
             latch, a disabled loop's first tick would turn the relays off and
             wipe the restored hand-set positions within one tick;
          3. loop off: heater forced off FIRST, unconditionally (never
             persisted), then fan/humidifier applied per the stored values;
          4. loop on: no output is touched — the loop drives from its first tick.

        Never raises into the startup path: a missing or corrupt file simply
        means "nothing to restore" and the state.py defaults stand.
        """
        data = self._load()

        # 1. Targets — mutate the shared dict in place, key by key.
        sp = data.get("setpoints")
        if isinstance(sp, dict):
            for key in ("temperature", "humidity"):
                if key in sp:
                    state.setpoints[key] = sp[key]
                    print(f"  [{state.handle}] runtime: restored setpoints.{key} = {sp[key]}")

        # 2/3/4. Loop flag (+ relay restore when it is off).
        # Default True mirrors the firmware's `data.get("control_enabled", True)`.
        enabled = data.get("control_enabled", True)
        state.control_enabled = enabled
        if enabled:
            print(f"  [{state.handle}] runtime: control loop restored ENABLED")
            # Loop on: touch no outputs — hysteresis drives them from the targets.
            return

        # Loop off: prime the latch so the restored positions below survive the
        # engine's first disabled tick (mirror of prime_control_enabled(False,
        # relays_latched=True)). Primed unconditionally when the loop comes
        # back off, exactly like the firmware.
        simulation.relays_latched = True
        print(f"  [{state.handle}] runtime: control loop restored DISABLED")

        # Heater first, unconditionally — it is never restored to ON.
        state._outputs["heater"] = False
        print(f"  [{state.handle}] runtime: heater held OFF (never persisted)")

        outs = data.get("outputs")
        if isinstance(outs, dict):
            for key in ("fan", "humidifier"):
                val = outs.get(key)
                if val is not None:
                    state._outputs[key] = val
                    print(f"  [{state.handle}] runtime: restored outputs.{key} = {val}")
        else:
            print(f"  [{state.handle}] runtime: no persisted outputs (relays stay off)")
