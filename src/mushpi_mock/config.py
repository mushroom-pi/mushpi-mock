"""
Configuration loading and validation.

NestJS mapping:
    This is like ConfigService + Joi validation schema combined.
    In NestJS you'd have:
        - ConfigModule.forRoot({ load: [config] })
        - A Joi schema to validate the loaded config
    Here we use Pydantic models for both validation and typing.

    The config can come from:
        1. A JSON file (--config flag)
        2. CLI flags (--count, --handles, --base-port)
        3. Sensible defaults (single unit on port 5000)
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class UnitConfig(BaseModel):
    """Configuration for a single mock Pico unit.

    NestJS equivalent: a DTO class with class-validator decorators.
    Pydantic's BaseModel gives us validation + typing for free.
    """

    handle: str = Field(default="mock-tent-1", description="Unit handle (must match server PicoUnit handle)")
    port: int = Field(default=5000, ge=1024, le=65535, description="Port to listen on")


class MockConfig(BaseModel):
    """Top-level configuration for the mock service.

    NestJS equivalent: the root config object validated by Joi in ConfigModule.
    """

    server_url: str = Field(
        default="http://localhost:3000",
        description="Server base URL (no trailing slash) for announce POST",
    )
    hub_secret: str = Field(
        default="mushpi-dev-secret",
        description="Shared secret sent as X-Pico-Secret header on announce",
    )
    units: list[UnitConfig] = Field(
        default_factory=lambda: [UnitConfig()],
        description="List of mock units to run",
    )


def load_config(path: str | None) -> MockConfig:
    """Load config from a JSON file, or return defaults.

    NestJS equivalent: ConfigService reads .env + validates with Joi.
    Here we read a JSON file and validate with Pydantic.

    If path is None, returns defaults (single unit on port 5000).
    If the file doesn't exist, prints a warning and returns defaults.
    """
    if path is None:
        return MockConfig()

    p = Path(path)
    if not p.exists():
        print(f"Config file not found: {path} — using defaults")
        return MockConfig()

    with open(p) as f:
        data = json.load(f)

    return MockConfig.model_validate(data)
