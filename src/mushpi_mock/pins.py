"""
Reserved-GPIO rules — the firmware's assignable-pin mirror.

NestJS mapping:
    A tiny validation helper, like the static validators a DTO would call.
    In the firmware these constants live in ``app/config_validator.py``; the
    server's setup-proxy util and this mock implement the SAME rule set as
    deliberately duplicated logic (root AGENTS.md: the duplicate-GPIO rule set
    is "semantics-locked" across firmware, server and mock). Any change to one
    must land in the other two.

What this module mirrors
    ``POST /setup`` rejects, with the firmware's exact named reasons:
      * non-whole-number values (and booleans — Python inherits CPython's
        ``bool ⊂ int``, the firmware tests it the same way);
      * the reserved GPIOs — GP0 (the force-provision jumper sampled at every
        boot) and GP23–GP25/GP29 (wired to the CYW43439 wireless chip);
      * anything else outside the assignable set **1–22, 26, 27, 28**.
    The strings below are copied verbatim from ``mushpi-grow/app/config_validator.py``
    so a tester sees the same refusal sentence on the mock as on a unit.

What this module deliberately does NOT mirror
    The firmware's separate, STRICTER boot rule — "any two roles sharing a GPIO
    in the effective config.json map is a fatal config-validation error". The
    mock has no boot-time config validation and must not gain one here; the
    request-merged duplicate rule lives in ``router.update_setup`` and is a
    different rule set. Do not fold the two together.
"""

from __future__ import annotations

from typing import Any

# Device roles in the firmware's canonical order (same tuple as app/api.py and
# app/config_validator.py) — the order POST /setup scans and refuses in.
PIN_ROLES: tuple[str, ...] = ("dht", "humidifier", "fan", "heater")

# Canonical firmware truth for user-assignable pins (constants, not config keys:
# an operator must not be able to config their way onto a reserved pin).
# Pico 2 W (RP2350 + CYW43439): valid user GPIOs are 1–22, 26, 27, 28.
VALID_USER_GPIO: frozenset[int] = frozenset(range(1, 23)) | frozenset({26, 27, 28})

# Named refusal sentences, copied verbatim from the firmware constants.
RESERVED_GPIO_REASONS: dict[int, str] = {
    0: "GP0 is reserved for the force-provision jumper (sampled at every boot)",
    23: "GP23 is reserved for the CYW43439 wireless chip",
    24: "GP24 is reserved for the CYW43439 wireless chip",
    25: "GP25 is reserved for the CYW43439 wireless chip",
    29: "GP29 is reserved for the CYW43439 wireless chip",
}

VALID_GPIO_HINT = "valid user GPIOs are 1-22, 26, 27 and 28"


def gpio_reject_reason(value: Any) -> str | None:
    """Return ``None`` when `value` is an assignable GPIO, else the named refusal.

    Mirrors ``gpio_reject_reason()`` in the firmware's ``app/config_validator.py``:
      - ``bool`` or non-``int``  → "must be a whole number"
        (booleans are tested first — otherwise ``True`` would pass as int ``1``)
      - in the assignable set    → accepted
      - reserved                 → that pin's reservation sentence
      - otherwise                → off-board refusal

    Args:
        value: the raw pin value as it arrived in the request body — deliberately
            NOT coerced to int by the request model, so a string ``"4"`` or a
            float ``4.5`` gets the firmware's whole-number refusal instead of
            silently passing validation.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return "must be a whole number"
    if value in VALID_USER_GPIO:
        return None
    if value in RESERVED_GPIO_REASONS:
        return RESERVED_GPIO_REASONS[value]
    return f"GP{value} is not a usable Pico 2 W GPIO"
