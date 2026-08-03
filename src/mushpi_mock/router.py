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

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .state import UnitState

router = APIRouter()


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
        }
    """

    enabled: bool | None = None
    period_s: int | None = Field(None, ge=1, description="Control loop period in seconds")
    hyst_temp: int | None = Field(None, ge=0, description="Temperature hysteresis degrees")
    hyst_hum: int | None = Field(None, ge=0, description="Humidity hysteresis percentage")


class PinsDto(BaseModel):
    """GPIO pin mapping for POST /setup."""

    dht: int | None = Field(None, ge=1, le=29)
    humidifier: int | None = Field(None, ge=1, le=29)
    fan: int | None = Field(None, ge=1, le=29)
    heater: int | None = Field(None, ge=1, le=29)


class SetupDto(BaseModel):
    """POST /setup body — GPIO pin mapping and relay polarity."""

    pins: PinsDto | None = None
    active_high: bool | None = None


class RebootDto(BaseModel):
    """POST /reboot body — soft or hard reboot."""

    type: str = "soft"


# ── Helper ────────────────────────────────────────────────────────────────


def _get_state(request: Request) -> UnitState:
    """Extract UnitState from the FastAPI app's state.

    NestJS equivalent: injecting a service via constructor DI.
    Here we use Starlette's app.state (set during app creation in main.py).
    """
    return request.app.state.unit_state  # type: ignore[attr-defined]


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


# ── Sensor endpoints ──────────────────────────────────────────────────────


@router.get("/sensors")
async def get_sensors(request: Request, force: str | None = None) -> dict[str, Any]:
    """Read DHT sensor values.

    The firmware supports ?force=1 to trigger an on-demand measurement.
    In the mock, force triggers an immediate simulation tick.
    """
    state = _get_state(request)
    if force:
        # Simulate a fresh reading
        from .simulation import SimulationEngine

        engine = SimulationEngine(state)
        await engine.tick()
    return state.get_sensor_reading()


# ── Setpoint endpoints ────────────────────────────────────────────────────


@router.get("/setpoints")
async def get_setpoints(request: Request) -> dict[str, int]:
    """Get current temperature and humidity setpoints."""
    state = _get_state(request)
    return dict(state.setpoints)


@router.post("/setpoints")
@router.put("/setpoints")
async def update_setpoints(request: Request, dto: SetpointsDto) -> dict[str, int]:
    """Update temperature and/or humidity setpoints.

    The firmware immediately re-evaluates hysteresis when setpoints change
    (if control is enabled). We don't simulate hysteresis in the mock.
    """
    state = _get_state(request)
    if dto.temperature is not None:
        state.setpoints["temperature"] = dto.temperature
    if dto.humidity is not None:
        state.setpoints["humidity"] = dto.humidity
    print(f"  [{state.handle}] Setpoints updated: {state.setpoints}")
    return dict(state.setpoints)


# ── Output endpoints ──────────────────────────────────────────────────────


@router.get("/outputs")
async def get_outputs(request: Request) -> dict[str, bool]:
    """Get current relay states (fan, humidifier, heater)."""
    state = _get_state(request)
    return dict(state._outputs)


@router.post("/outputs")
@router.put("/outputs")
async def update_outputs(request: Request, dto: OutputsDto) -> dict[str, Any]:
    """Set relay states.

    The firmware requires ALL three fields (fan, humidifier, heater).
    We mirror that validation — missing fields return 422.
    """
    state = _get_state(request)

    # The firmware requires all three fields — mirror that validation
    errors: list[str] = []
    if dto.humidifier is None:
        errors.append('the body needs to include "humidifier" status (true/false)')
    if dto.fan is None:
        errors.append('the body needs to include "fan" status (true/false)')
    if dto.heater is None:
        errors.append('the body needs to include "heater" status (true/false)')
    if errors:
        raise HTTPException(status_code=422, detail=errors[0])

    state._outputs["fan"] = dto.fan  # type: ignore[assignment]
    state._outputs["humidifier"] = dto.humidifier  # type: ignore[assignment]
    state._outputs["heater"] = dto.heater  # type: ignore[assignment]

    print(f"  [{state.handle}] Outputs updated: {state._outputs}")
    return {
        "ok": True,
        "fan": dto.fan,
        "humidifier": dto.humidifier,
        "heater": dto.heater,
    }


# ── Control endpoints ─────────────────────────────────────────────────────


@router.get("/control")
async def get_control(request: Request) -> dict[str, Any]:
    """Get control loop state: enabled flag + hysteresis configuration.

    Returns enabled, period_s, hyst_temp, hyst_hum — matching the firmware's
    control dict that the server stores when polling GET /.
    """
    state = _get_state(request)
    return {
        "enabled": state.control_enabled,
        "period_s": state.control_config["period_s"],
        "hyst_temp": state.control_config["hyst_temp"],
        "hyst_hum": state.control_config["hyst_hum"],
    }


@router.post("/control")
@router.put("/control")
async def update_control(request: Request, dto: ControlDto) -> dict[str, Any]:
    """Enable/disable the control loop and/or update hysteresis parameters.

    The firmware calls relays_off() when disabling. We just update the flag.
    All fields are optional — only provided fields are updated (partial PATCH).
    """
    state = _get_state(request)
    if dto.enabled is not None:
        state.control_enabled = dto.enabled
    if dto.period_s is not None:
        state.control_config["period_s"] = dto.period_s
    if dto.hyst_temp is not None:
        state.control_config["hyst_temp"] = dto.hyst_temp
    if dto.hyst_hum is not None:
        state.control_config["hyst_hum"] = dto.hyst_hum
    print(f"  [{state.handle}] Control updated: enabled={state.control_enabled}, "
          f"config={state.control_config}")
    return {
        "enabled": state.control_enabled,
        "period_s": state.control_config["period_s"],
        "hyst_temp": state.control_config["hyst_temp"],
        "hyst_hum": state.control_config["hyst_hum"],
    }


# ── Setup endpoints ───────────────────────────────────────────────────────


@router.get("/setup")
async def get_setup(request: Request) -> dict[str, Any]:
    """Get GPIO pin mapping and relay polarity."""
    state = _get_state(request)
    return dict(state.devices)


@router.post("/setup")
@router.put("/setup")
async def update_setup(request: Request, dto: SetupDto) -> dict[str, Any]:
    """Update GPIO pin mapping and/or relay polarity.

    The firmware persists this to flash (config.json). The mock just
    updates in-memory state.
    """
    state = _get_state(request)

    if dto.active_high is not None:
        state.devices["active_high"] = dto.active_high

    if dto.pins is not None:
        pins = dto.pins
        if pins.dht is not None:
            state.devices["pins"]["dht"] = pins.dht
        if pins.humidifier is not None:
            state.devices["pins"]["humidifier"] = pins.humidifier
        if pins.fan is not None:
            state.devices["pins"]["fan"] = pins.fan
        if pins.heater is not None:
            state.devices["pins"]["heater"] = pins.heater

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


@router.get("/")
async def get_all(request: Request, force: str | None = None) -> dict[str, Any]:
    """Full system snapshot — everything the server's cron poller needs.

    This is the most important endpoint: the server polls GET / every 60s
    to create Readings rows. The response shape must match exactly what
    ReadingsService.createFromDeviceResponse() expects.
    """
    state = _get_state(request)
    if force:
        from .simulation import SimulationEngine

        engine = SimulationEngine(state)
        await engine.tick()
    return state.get_full_snapshot()
