"""
FastAPI router — all Pico API endpoints.

NestJS mapping:
    This is like a @Controller() with route decorators. In NestJS:
        @Controller()
        export class PicoController {
            @Get('sensors')
            getSensors() { return this.state.getSensorReading(); }

            @Post('setpoints')
            setSetpoints(@Body() dto: SetpointsDto) { ... }
        }

    In FastAPI, we use APIRouter + @router.get() / @router.post():
        router = APIRouter()

        @router.get("/sensors")
        def get_sensors(request: Request):
            state: UnitState = request.app.state.unit_state
            return state.get_sensor_reading()

    Key differences from NestJS:
        - No DI — we get state from request.app.state (Starlette app state)
        - No DTO classes — Pydantic models on the request body handle validation
        - No @UsePipes() — Pydantic validation is automatic (like a global ValidationPipe)
        - No @UseGuards() — we'd use FastAPI dependencies (Depends) for auth

    The firmware uses POST for mutations (POST /setpoints, POST /outputs, etc.).
    We also accept PUT for convenience, matching the roadmap spec.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__ as _MOCK_VERSION
from . import unit_page
from .pins import PIN_ROLES, VALID_GPIO_HINT, gpio_reject_reason
from .runtime_state import RuntimeStateStore
from .simulation import SimulationEngine
from .state import _API_VERSION, _SOFTWARE_VERSION, UnitState

router = APIRouter()

# AP provisioning is view-only: every control POST is refused with this 403 body
# before any side effect. Copied verbatim from the firmware's `_AP_VIEW_ONLY_ERROR`
# (app/api.py) so the wire body matches a unit's. The mock presents as a STA unit
# (`UnitState.mode` is always "sta" — there is no AP entry point), so this guard
# is mirrored contract shape rather than reachable behaviour; it exists so the
# mirrored surface matches the spec's documented 403s as real code.
_AP_VIEW_ONLY_ERROR = {
    "ok": False,
    "error": "controls are only available in STA mode - AP provisioning is view-only",
}


def _ap_view_only() -> JSONResponse:
    """The firmware's AP-mode refusal: 403 + `{"ok": false, "error": …}`.

    `JSONResponse` (not `HTTPException`) so the body is the firmware's shape —
    `HTTPException` would emit `{"detail": ...}` instead.
    """
    return JSONResponse(status_code=403, content=_AP_VIEW_ONLY_ERROR)


# The POST /provision STA-refusal body. Copied verbatim from the firmware's
# `_provision` mode guard (app/api.py): a connected (not-"ap") unit is rejected
# with this 403 BEFORE any body parsing, config mutation, save, or reboot. The
# mock always presents as a STA unit (`UnitState.mode` is always "sta" — there
# is no AP entry point), so this is not a guarded branch here but the route's
# invariant response: mirror the firmware's body exactly, never a mock-specific
# message. A real AP success path (write wifi creds, reboot) is deliberately
# absent — the mock has no AP path to reach it from.
_AP_ONLY_PROVISION_ERROR = {
    "ok": False,
    "error": "provisioning is only available in AP mode",
}



# ── Request body DTOs ─────────────────────────────────────────────────────
# NestJS equivalent: DTO classes with class-validator decorators.
# Pydantic BaseModel gives us validation + typing automatically.
# FastAPI rejects requests with wrong types (like ValidationPipe in NestJS).


class SetpointsDto(BaseModel):
    """POST /setpoints body — target temperature and humidity.

    NestJS equivalent:
        class SetpointsDto {
            @IsInt() temperature: number;
            @IsInt() humidity: number;
        }
    """

    temperature: int | None = Field(None, description="Target temperature in degrees C")
    humidity: int | None = Field(None, description="Target humidity percentage")


class OutputsDto(BaseModel):
    """POST /outputs body — relay on/off states.

    NestJS equivalent:
        class OutputsDto {
            @IsBoolean() fan: boolean;
            @IsBoolean() humidifier: boolean;
            @IsBoolean() heater: boolean;
        }
    """

    fan: bool | None = None
    humidifier: bool | None = None
    heater: bool | None = None


class ControlDto(BaseModel):
    """POST /control body — enable/disable the control loop + hysteresis config.

    NestJS equivalent:
        class ControlDto {
            @IsBoolean() enabled: boolean;
            @IsOptional() @IsInt() period_s?: number;
            @IsOptional() @IsInt() hyst_temp?: number;
            @IsOptional() @IsInt() hyst_hum?: number;
            @IsOptional() @IsInt() humidity_deadband?: number;
            @IsOptional() @IsInt() temperature_deadband?: number;
            @IsOptional() @IsInt() min_runtime?: number;
        }
    """

    enabled: bool | None = None
    period_s: int | None = Field(None, ge=1, description="Control loop period in seconds")
    hyst_temp: int | None = Field(None, ge=0, description="Temperature hysteresis degrees")
    hyst_hum: int | None = Field(None, ge=0, description="Humidity hysteresis percentage")
    humidity_deadband: int | None = Field(None, ge=0, description="Humidity deadband (v0.8.0+)")
    temperature_deadband: int | None = Field(None, ge=0, description="Temperature deadband (v0.8.0+)")
    min_runtime: int | None = Field(None, ge=0, description="Minimum relay runtime seconds (v0.8.0+)")


class RebootDto(BaseModel):
    """POST /reboot body — soft or hard reboot."""

    type: str = "soft"


class VersionInfoDto(BaseModel):
    """GET /version response — the mock's dual version identity.

    NestJS equivalent: a response DTO (class with class-validator, here
    Pydantic so FastAPI embeds it as the OpenAPI response schema).

    Mock-only endpoint (not part of the mirrored Pico API): separates the
    mock tool's own service version from the firmware version it represents,
    per mushpi-docs/versioning.md §6 (dual-versioning).
    """

    mock_version: str = Field(
        description="The mock tool's own version (pyproject.toml `version`)"
    )
    represented_firmware_version: str = Field(
        description="Firmware version the mock emulates (mirrors grow _SOFTWARE_VERSION)"
    )
    represented_api_version: int = Field(
        description="Pico↔Server REST contract generation the mock implements "
        "(mirrors grow _API_VERSION)"
    )


# ── Helper ────────────────────────────────────────────────────────────────


def _get_state(request: Request) -> UnitState:
    """Extract UnitState from the FastAPI app's state.

    NestJS equivalent: injecting a service via constructor DI.
    Here we use Starlette's app.state (set during app creation in main.py).
    """
    return request.app.state.unit_state  # type: ignore[attr-defined]


def _get_store(request: Request) -> RuntimeStateStore:
    """Extract this unit's runtime-state store (wired in main.create_app).

    NestJS equivalent: the repository service injected into the controller.
    Sole consumers are the three mutation handlers below — the firmware rule
    that only the three POST handlers write runtime.json maps to "only
    update_setpoints / update_outputs / update_control call the save_*
    methods". The simulation module must never acquire this store (loop-driven
    switching never writes storage).
    """
    return request.app.state.runtime_store  # type: ignore[attr-defined]


def _get_engine(request: Request) -> SimulationEngine:
    """The unit's LONG-LIVED simulation engine (wired in main.create_app).

    Force-tick handlers must use this one engine, never a throwaway: the
    disabled-relay one-shot lives on the engine instance (mirroring
    control_loop()'s `relays_latched`), and a fresh engine per request would
    re-run the one-off relay wipe on every `?force=1`, wiping hand-set
    positions a real unit holds.
    """
    return request.app.state.simulation  # type: ignore[attr-defined]


# ── Monitoring endpoints ──────────────────────────────────────────────────


@router.get("/ping")
async def ping() -> Any:
    """Liveness check — empty 200 response.

    NestJS equivalent: @Get('ping') with no return value.
    The server uses this to check if the unit is reachable.
    """
    return


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """System health snapshot — RAM, filesystem, WiFi, MCU temp, uptime.

    The firmware's /health includes system_snapshot() data plus uptime info.
    """
    state = _get_state(request)
    snap = state.get_health_snapshot()
    snap["uptime"] = state.get_uptime_info()
    return snap


@router.get("/system")
async def system(request: Request) -> dict[str, Any]:
    """System information — board, MicroPython version, software version, WiFi.

    The firmware's /system includes init_system_info() data plus uptime info.
    """
    state = _get_state(request)
    info = dict(state._system_info)
    info["uptime"] = state.get_uptime_info()
    return info


@router.get("/version", response_model=VersionInfoDto)
async def version() -> VersionInfoDto:
    """Dual-version identity — mock's own version vs the firmware it represents.

    Mock-only endpoint: the real Pico firmware does NOT serve /version
    (it is absent from spec/openapi.yaml — do not add it there). This
    exists because the mock is a mirror *and* an independent service,
    so it carries two version identities (mushpi-docs/versioning.md §6).

    NestJS mapping: @Get('version') returning a response DTO. FastAPI
    serialises the Pydantic model and documents it under /docs.
    """
    return VersionInfoDto(
        mock_version=_MOCK_VERSION,
        represented_firmware_version=_SOFTWARE_VERSION,
        represented_api_version=_API_VERSION,
    )


# ── Unit page endpoints ──────────────────────────────────────────────────


@router.get("/ui", response_model=None)
async def get_ui(request: Request) -> Response:
    """The on-unit status-and-control page — always ``text/html``.

    Mirrors the firmware's ``GET /ui``: the vendored page (a byte-identical copy
    of ``mushpi-grow/app/unit.html``) rendered for THIS unit, with the mock's
    simulated labels applied at render time (see ``unit_page``). The bootstrap is
    substituted per unit — ``DEVNAME`` becomes this unit's handle and the page's
    relative fetches (`/`, `/control`, `/static/unit.js`, …) address this unit's
    own origin — so every running mock unit serves its own page over its own
    state, with no hub and no internet required.

    NestJS mapping: a controller returning a view instead of a DTO — here we hand
    back a raw ``Response`` (``response_model=None``) because the route does not
    answer JSON.
    """
    state = _get_state(request)
    return unit_page.unit_page_response(state.mode, state.handle)


@router.get("/static/{filename}", response_model=None)
async def get_static(filename: str) -> Response:
    """Same-origin page assets — ``unit.js`` and ``favicon.svg``.

    Mirrors the firmware's ``GET /static/<string:filename>``. The `{filename}`
    path parameter matches a single path component and the handler resolves it
    only through a closed allowlist, so no filesystem path is ever built from the
    request string — the mock's equivalent of the firmware's `<string:…>` segment
    type that keeps a path-traversal shape out of the route. Anything else (and
    the page itself) is a 404 with the firmware's `{"ok": false, "error":
    "static asset missing"}` body. Neither the hub nor the server consumes these
    paths — they are page surface only.
    """
    return unit_page.static_response(filename)


# ── Provisioning endpoints ───────────────────────────────────────────────


@router.get("/provision", response_model=None)
async def get_provision(request: Request) -> Response:
    """The Wi-Fi credential setup form — always ``text/html``.

    Mirrors the firmware's ``GET /provision``, served in BOTH operating modes
    (the unit page's offline banner links here). The vendored copy of
    ``mushpi-grow/app/provision.html`` is served unmodified except the
    firmware's own ``DEVNAME`` injection — no mock seam edits. The form's
    submit hits the 403 below, exactly what a connected real unit answers, so
    the page needs no simulated label.

    NestJS mapping: a controller returning a view instead of a DTO —
    ``response_model=None`` because the route does not answer JSON.
    """
    state = _get_state(request)
    return unit_page.provision_page_response(state.handle)


@router.post("/provision", response_model=None)
async def post_provision() -> JSONResponse:
    """Wi-Fi provisioning — the firmware's STA refusal: 403, no state change.

    The firmware's handler checks the operating mode FIRST — before any body
    parsing, config mutation, save, or reboot — and rejects a connected (STA)
    unit with the exact ``{"ok": false, "error": "provisioning is only
    available in AP mode"}`` body. The mock has no AP path, so that refusal is
    its invariant response here, before anything else.

    Deliberately NO request DTO: a Pydantic body model would fire FastAPI's
    422 before the guard runs, while the firmware 403s every POST from a
    connected unit regardless of body. ``JSONResponse`` (not
    ``HTTPException``) keeps the firmware's wire shape (``HTTPException``
    would emit ``{"detail": ...}``).
    """
    return JSONResponse(status_code=403, content=_AP_ONLY_PROVISION_ERROR)


# ── Sensor endpoints ──────────────────────────────────────────────────────


@router.get("/sensors")
async def get_sensors(request: Request, force: str | None = None) -> dict[str, Any]:
    """Read DHT sensor values.

    The firmware supports ?force=1 to trigger an on-demand measurement.
    In the mock, force triggers an immediate simulation tick on the unit's
    LONG-LIVED engine — never a throwaway one, so the disabled-relay one-shot
    latch is continuous across forced reads (see _get_engine).
    """
    state = _get_state(request)
    if force:
        # Simulate a fresh reading
        await _get_engine(request).tick()
    return state.get_sensor_reading()


# ── Setpoint endpoints ────────────────────────────────────────────────────


@router.get("/setpoints")
async def get_setpoints(request: Request) -> dict[str, int]:
    """Get current temperature and humidity setpoints."""
    state = _get_state(request)
    return dict(state.setpoints)


@router.post("/setpoints", response_model=None)
@router.put("/setpoints", response_model=None)
async def update_setpoints(request: Request, dto: SetpointsDto) -> dict[str, int] | JSONResponse:
    """Change setpoints (persisted — survives a mock stop/start).

    Mirrors the spec's POST /setpoints description, adapted to the mock's
    storage: updates one or both setpoints and persists the applied pair to
    this unit's runtime state file, so the targets survive a stop/start of the
    mock process — the file equivalent of the device writing runtime.json to
    flash and surviving a reboot. A write failure is logged on the unit and
    does NOT fail the request — the response reports the live in-memory state.

    The next simulation tick (within tick_interval seconds, or an immediate
    one via ?force=1) re-evaluates hysteresis against the new targets when the
    control loop is enabled.

    AP mode: refused with 403 BEFORE any mutation (the firmware's view-only
    guard — mirrored contract shape; the mock always presents as STA).
    """
    state = _get_state(request)
    if state.mode == "ap":
        return _ap_view_only()
    if dto.temperature is not None:
        state.setpoints["temperature"] = dto.temperature
    if dto.humidity is not None:
        state.setpoints["humidity"] = dto.humidity
    print(f"  [{state.handle}] Setpoints updated: {state.setpoints}")

    # Persist the grower-chosen targets — the full current pair read back from
    # state (not the partial body), so a one-key POST persists both values,
    # exactly like the firmware's save_setpoints(setpoints["temperature"],
    # setpoints["humidity"]). This is one of ONLY three mutation handlers that
    # write the state file; the simulation never does.
    try:
        _get_store(request).save_setpoints(state.setpoints["temperature"], state.setpoints["humidity"])
    except OSError as e:
        print(f"  [{state.handle}] setpoints: persist failed: {e}")

    return dict(state.setpoints)


# ── Output endpoints ──────────────────────────────────────────────────────


@router.get("/outputs")
async def get_outputs(request: Request) -> dict[str, bool]:
    """Get current relay states (fan, humidifier, heater)."""
    state = _get_state(request)
    return dict(state._outputs)


@router.post("/outputs", response_model=None)
@router.put("/outputs", response_model=None)
async def update_outputs(request: Request, dto: OutputsDto) -> dict[str, Any] | JSONResponse:
    """Change outputs status (hand-set fan/humidifier persisted; heater never).

    Mirrors the firmware's `_setoutputs`: individual keys are OPTIONAL — only the
    keys present in the body are applied (bool-coerced like the firmware, so an
    explicit `null` means OFF), omitted relays keep their current state, and at
    least one key is required. The response always reports all three relays read
    back from the unit's state — that is what the page shows after a save ("the
    state the mock reports back").

    Persistence (spec mirror): the applied fan and humidifier states are written
    to this unit's runtime state file — they are restored on a stop/start when
    the control loop is off. The heater NEVER persists: it always comes back
    OFF after a restart (same safety rule as the device; save_outputs has no
    heater parameter by design). A write failure is logged on the unit and does
    NOT fail the request. While the loop is on it reclaims all three relays on
    the next tick — the saved pair matters only for the loop-off restore, again
    exactly like the device.

    The unit page's Devices card sends only the *changed* switches, so a body
    like `{"humidifier": true}` is the normal case. An earlier revision required
    all three fields and rejected exactly that, which broke hand-switching.

    Empty body: 422 with the firmware's `{"ok": false, "error": ...}` shape
    (JSONResponse, not HTTPException — the page surfaces the reason).
    AP mode: refused with 403 BEFORE any relay write (the firmware's view-only
    guard — mirrored contract shape; the mock always presents as STA).
    """
    state = _get_state(request)
    if state.mode == "ap":
        return _ap_view_only()

    # Presence, not truthiness: `model_fields_set` is the set of keys this
    # request explicitly carried — the Pydantic equivalent of the firmware's
    # `if k in data` membership test.
    valid_keys = ("humidifier", "fan", "heater")
    provided = [key for key in valid_keys if key in dto.model_fields_set]
    if not provided:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": 'the body must include at least one of: "humidifier", "fan", "heater"',
            },
        )

    # Apply only the provided relays; omitted relays keep their current state.
    for key in provided:
        state._outputs[key] = bool(getattr(dto, key))  # firmware: bool(provided[k])

    print(f"  [{state.handle}] Outputs updated: {state._outputs}")

    # Persist the hand-set pair — read from state (the applied values), not the
    # partial-body locals, so a single-key POST persists the full pair. The
    # heater is structurally excluded: save_outputs takes no heater parameter.
    try:
        _get_store(request).save_outputs(state._outputs["fan"], state._outputs["humidifier"])
    except OSError as e:
        print(f"  [{state.handle}] outputs: persist failed: {e}")

    return {
        "ok": True,
        "fan": state._outputs["fan"],
        "humidifier": state._outputs["humidifier"],
        "heater": state._outputs["heater"],
    }


# ── Control endpoints ─────────────────────────────────────────────────────


@router.get("/control")
async def get_control(request: Request) -> dict[str, bool]:
    """Get control loop status — enabled flag only.

    The real Pico's GET /control returns {"enabled": true|false}.
    The full control_config (hysteresis params, period, etc.) is internal
    state used by POST /control, not exposed in the GET response.
    """
    state = _get_state(request)
    return {"enabled": state.control_enabled}


@router.post("/control", response_model=None)
@router.put("/control", response_model=None)
async def update_control(request: Request, dto: ControlDto) -> dict[str, Any] | JSONResponse:
    """Toggle control loop (persisted — the loop restarts as it was left).

    Mirrors the spec's POST /control description, adapted to the mock's
    storage: the enabled flag is written to this unit's runtime state file, so
    after a stop/start the loop comes back in the state it was left in. When
    the loop comes back off, the persisted hand-set fan/humidifier states are
    restored too (the heater always comes back OFF); when it comes back on, no
    relay state is restored and the loop drives the relays from the persisted
    setpoints. A write failure is logged on the unit and does NOT fail the
    request.

    Disabling turns all relays off immediately in this handler (the firmware's
    synchronous `relays_off()`), then the engine's one-shot latch holds them
    off while the loop stays disabled — after that one-off pass, hand-set
    positions made via POST /outputs survive, exactly like a unit whose loop
    is off. The disable-off is an actuation change, NOT a persistence event:
    the saved outputs keep the last grower-hand-set pair.

    The hysteresis config fields (period_s, hyst_*, deadbands, min_runtime)
    stay in-memory only — the device loads them from config.json at boot; they
    are not part of runtime state and are lost on a mock restart. All fields
    are optional here — only provided fields are updated (partial PATCH).

    AP mode: refused with 403 BEFORE the enable flag changes and before relays
    are touched (the firmware's view-only guard — mirrored contract shape; the
    mock always presents as STA).
    """
    state = _get_state(request)
    if state.mode == "ap":
        return _ap_view_only()
    if dto.enabled is not None:
        state.control_enabled = dto.enabled
        # Persist the loop switch so the grower's choice survives a restart.
        try:
            _get_store(request).save_control_enabled(dto.enabled)
        except OSError as e:
            print(f"  [{state.handle}] control: persist failed: {e}")
    if dto.period_s is not None:
        state.control_config["period_s"] = dto.period_s
    if dto.hyst_temp is not None:
        state.control_config["hyst_temp"] = dto.hyst_temp
    if dto.hyst_hum is not None:
        state.control_config["hyst_hum"] = dto.hyst_hum
    if dto.humidity_deadband is not None:
        state.control_config["humidity_deadband"] = dto.humidity_deadband
    if dto.temperature_deadband is not None:
        state.control_config["temperature_deadband"] = dto.temperature_deadband
    if dto.min_runtime is not None:
        state.control_config["min_runtime"] = dto.min_runtime

    # Synchronous relays_off when disabling — firmware parity (io.relays_off()
    # runs inside the POST handler, before the loop's next pass). In-memory
    # only: the persisted outputs keep the last hand-set pair, never this
    # forced-off, matching the device exactly.
    if dto.enabled is False:
        state._outputs.update({"fan": False, "humidifier": False, "heater": False})
        print(f"  [{state.handle}] Control disabled — relays off")

    print(f"  [{state.handle}] Control updated: enabled={state.control_enabled}, "
          f"config={state.control_config}")
    return {
        "enabled": state.control_enabled,
        **state.control_config,
    }


# ── Setup endpoints ───────────────────────────────────────────────────────


@router.get("/setup")
async def get_setup(request: Request) -> dict[str, Any]:
    """Get GPIO pin mapping and relay polarity."""
    state = _get_state(request)
    return dict(state.devices)


@router.post("/setup", response_model=None)
@router.put("/setup", response_model=None)
async def update_setup(request: Request) -> dict[str, Any] | JSONResponse:
    """Update GPIO pin mapping and/or relay polarity (full-parity validation).

    The firmware persists this to flash (config.json). The mock just
    updates in-memory state — and, unlike a unit, that state is lost when the
    mock stops (disclosed on the page).

    The body is read RAW (`await request.json()`), not through a Pydantic DTO.
    The firmware validates every pin in-handler with its `gpio_reject_reason()`
    and shapes its own `400` bodies, so mirroring that behaviour means the same
    values must reach the same Python code — a `SetupDto` with `int` fields would
    pre-empt that with FastAPI's `422 {"detail": ...}`, which is why reserved
    pins like GP23 (inside the old `le=29` bound) were never refused at all and
    GP0 got the wrong shape. This is the deliberate closure of the pre-existing
    422-vs-400 fidelity note: the requirement cannot be met while Pydantic owns
    the pin type.

    Rule order mirrors the firmware's `_setdevices`:
      1. non-dict body → 400; `pins` present but not a dict → 400
         `"pins must be a dict"`;
      2. per-role `gpio_reject_reason()` in PIN_ROLES order — a non-whole number,
         a reserved GPIO (GP0, GP23–25, GP29) or an off-board value → 400
         `"pins.<key> - <reason> (valid user GPIOs are 1-22, 26, 27 and 28)"`;
      3. the merged-view duplicate rule — a request that would leave two
         functions on one GPIO is rejected with 400
         `"GPIO <pin> is already assigned to <device>"`, checked against a
         merged view (current mapping overlaid with this request's keys) so
         re-assigning a device to its own pin is a no-op, a pin freed by another
         device in the same request is free, and pre-existing duplicates this
         request does not touch pass;
      4. only then `active_high` (bool-coerced), then the pin writes.

    All checks run BEFORE any mutation, so a refused request leaves the mapping
    unchanged. The semantics of rules 2–3 are deliberately duplicated (firmware
    `app/api.py` + `app/config_validator.py`, server setup proxy, this mock) and
    must stay locked together. The firmware's separate, STRICTER boot rule (two
    roles sharing a GPIO in the effective config map is fatal) is NOT mirrored
    here — the mock has no boot-time config validation.

    `JSONResponse` (not `HTTPException`) carries the 400: HTTPException would
    emit `{"detail": ...}` while the firmware emits `{"ok": False, "error": ...}`
    — the mock must match the wire body. `response_model=None` lets FastAPI
    accept the `dict | JSONResponse` return annotation.

    AP mode: refused with 403 BEFORE any pin remap, polarity mutation or
    config write (the firmware's view-only guard — mirrored contract shape; the
    mock always presents as STA).
    """
    state = _get_state(request)
    if state.mode == "ap":
        return _ap_view_only()

    # Raw body, exactly like the firmware's `req.json`. Invalid JSON or a
    # non-object body is a 400 with the firmware's `{"ok": False, "error": ...}`
    # shape (the firmware's own outer try/except does the same).
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid JSON body"},
        )
    if not isinstance(data, dict):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "body must be a dict"},
        )

    if "pins" in data:
        pins = data["pins"]
        if not isinstance(pins, dict):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "pins must be a dict"},
            )
        # 2. per-role assignable-GPIO check, firmware scan order (first hit wins).
        for key in PIN_ROLES:
            if key in pins:
                reason = gpio_reject_reason(pins[key])
                if reason is not None:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "ok": False,
                            "error": f"pins.{key} - {reason} ({VALID_GPIO_HINT})",
                        },
                    )
        # 3. merged-view duplicate rule (incoming keys only, same fixed order).
        incoming: dict[str, int] = {key: pins[key] for key in PIN_ROLES if key in pins}
        merged = {**state.devices["pins"], **incoming}
        for key in incoming:
            pin = incoming[key]
            for other in PIN_ROLES:
                if other != key and merged.get(other) == pin:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "ok": False,
                            "error": f"GPIO {pin} is already assigned to {other}",
                        },
                    )

    # 4. active_high only after every pin check passed (firmware ordering).
    if "active_high" in data:
        state.devices["active_high"] = bool(data["active_high"])

    if "pins" in data and isinstance(data["pins"], dict):
        for key in PIN_ROLES:
            if key in data["pins"]:
                # Values are validated ints by now (gpio_reject_reason above).
                state.devices["pins"][key] = data["pins"][key]

    print(f"  [{state.handle}] Setup updated: {state.devices}")
    return dict(state.devices)



# ── Reboot endpoint ───────────────────────────────────────────────────────


@router.post("/reboot")
async def reboot(request: Request, dto: RebootDto) -> dict[str, str]:
    """Trigger a mock reboot.

    The real Pico schedules a delayed reboot so the HTTP response can
    flush first. The mock just returns the response (no actual reboot).
    """
    if dto.type not in ("soft", "hard"):
        raise HTTPException(status_code=400, detail="type must be 'soft' or 'hard'")

    state = _get_state(request)
    print(f"  [{state.handle}] Reboot requested ({dto.type}) — mock ignores this")
    return {"message": "Reboot initiated", "type": dto.type}


# ── Root endpoint (full snapshot) ─────────────────────────────────────────


@router.get("/", response_model=None)
async def get_all(request: Request, force: str | None = None) -> dict[str, Any] | Response:
    """Full system snapshot — everything the server's cron poller needs.

    This is the most important endpoint: the server polls GET / every 60s
    to create Readings rows. The response shape must match exactly what
    ReadingsService.createFromDeviceResponse() expects, including the additive
    `hub: {configured, connected}` object (connected = the hub acknowledged the
    MOST RECENT announce — not live reachability).

    Content negotiation mirrors the firmware's `_get_all` **verbatim, strict on
    purpose**: the page is returned only when the request's Accept header
    contains `text/html` AND does not mention `application/json` — everything
    else keeps getting JSON. The hub's axios poll sends
    `application/json, text/plain, */*` and must keep getting JSON; a browser
    navigating here gets the same page `GET /ui` serves. The comparison is a
    plain case-sensitive substring test on the header value, exactly as the
    firmware does it, so the mock can never diverge from a unit on who gets the
    page — including not "improving" the heuristic.

    `?force=1` applies to the JSON snapshot only (it drives the simulation
    tick for a fresh reading); it is irrelevant to serving the static page.

    NestJS analogy: one route, two representations — like a controller method
    returning either a serialized DTO or a rendered view based on `@Accepts()`.
    `response_model=None` is required because the handler may return a raw
    `Response`.
    """
    state = _get_state(request)

    accept = request.headers.get("accept") or ""
    if "text/html" in accept and "application/json" not in accept:
        return unit_page.unit_page_response(state.mode, state.handle)

    if force:
        await _get_engine(request).tick()
    return state.get_full_snapshot()

