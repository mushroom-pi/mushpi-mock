# syntax=docker/dockerfile:1

###############################################################################
# mushpi-mock image — the simulated Pico units of the demo stack (a FastAPI
# service mirroring the firmware's REST API, one uvicorn per unit).
#
# Who builds this and who consumes it: this repository's own publish workflow
# (.github/workflows/publish.yml) is the ONLY publisher — it runs on a `v*`
# tag push (and on a manual dispatch of an existing tag), compiles the tagged
# source here, and pushes a pinned release tag plus an immutable commit tag to
# GHCR. `mushpi-demo` consumes that published tag and nothing else: no demo
# host ever builds this image. `mushpi-demo`'s docker-compose.local.yml is the
# only path that does, building from the developer's own working copy of this
# repo.
#
# Context filtering lives in `Dockerfile.dockerignore`: BuildKit prefers
# `<dockerfile>.dockerignore` over the shared `.dockerignore` and replaces it
# entirely, so the ignore set travels with this Dockerfile. It must never
# exclude a path the COPY lines below read: pyproject.toml, uv.lock, README.md,
# LICENSE and src/.
###############################################################################

FROM python:3.12-slim-bookworm

# uv straight from its official image (binary COPY — no pip bootstrap). The
# lockfile, not the uv version, is what pins dependencies (--frozen below).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# `node` at UID/GID 1000, matching the hub image's runtime user: the demo
# stack bind-mounts ONE host directory into both containers at /data, so the
# server (which creates the SQLite file) and the mock (which seeds it) need
# consistent ownership. The python base ships no 1000 assignment (verified).
RUN groupadd --gid 1000 node \
    && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home node

WORKDIR /app

# Project metadata first (layer-cached), then the package source. --chown so
# the runtime user owns the tree before uv sync; the untracked local
# `config.json` is never copied (it may hold a real secret) — the demo stack
# bind-mounts the generated one at /app/config.json read-only instead.
COPY --chown=node:node \
    pyproject.toml \
    uv.lock \
    README.md \
    LICENSE \
    ./
COPY --chown=node:node src ./src

# /app must be node-writable for the .venv uv sync creates in place; /data
# mirrors the hub image's pre-created, node-owned volume mount point so a
# no-volume `docker run` still boots (seed errors are logged, never fatal).
RUN mkdir -p /data && chown node:node /app /data

# Use the base image's Python 3.12 (it satisfies the repo's `.python-version`
# pin) instead of letting uv download its managed CPython into the image.
ENV UV_PYTHON_PREFERENCE=only-system

# Reproducible dependency install from the lockfile, runtime deps only — built
# wheels included. Runs as node so everything under /app stays node-owned.
USER node
RUN uv sync --frozen --no-dev

# The console script `mushpi-mock` (= mushpi_mock.main:cli) lives in the venv.
ENV PATH="/app/.venv/bin:$PATH"

# Guard, not a self-check: the script name and its `module:attr` target are
# facts owned by `pyproject.toml`, so a rename there would otherwise surface
# as a container that dies at start. Invoking it proves the console script
# resolves and imports at BUILD time (`--help` on the click group needs no
# config and exits 0).
RUN mushpi-mock --help

# Units listen on the ports carried by the mounted config (the demo stack uses
# 5000-5004). No baked config, no baked data — the compose file supplies both.
EXPOSE 5000-5004

ENTRYPOINT ["mushpi-mock"]
CMD ["start"]
