"""Mock Pico growing unit — mirrors mushpi-grow REST API with synthetic sensor data.

Two version identities live here (dual-versioning, see mushpi-docs/versioning.md §6):

- ``__version__``  — the mock tool's OWN service version, resolved from the
  installed package metadata (``pyproject.toml`` → ``version``). NestJS analogy:
  the ``version`` field of ``package.json``, read at runtime.
- ``state._SOFTWARE_VERSION`` — the firmware version the mock REPRESENTS
  (must equal mushpi-grow ``_SOFTWARE_VERSION``). Exposed via ``GET /version``
  so demos can tell the two apart.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Normal path: the package is installed in the venv (uv run / uv sync),
    # so pyproject.toml's version is discoverable as distribution metadata.
    __version__: str = version("mushpi-mock")
except PackageNotFoundError:
    # Bare source checkout without an installed dist — keep the service
    # importable, but make the gap obvious instead of silently lying.
    __version__ = "0.0.0+unknown"
