"""
Server announcement — POST /v1/pico-units/announce on startup.

NestJS mapping:
    The real Pico firmware announces itself to the server on boot.
    This module replicates that behavior using httpx (Python's async
    HTTP client, similar to axios in NestJS).

    The retry pattern uses exponential backoff: 5s → 10s → 30s → 60s cap.
    This mirrors the real Pico's WiFi reconnection behavior (see
    announce_then_retry_once in mushpi-grow/app/announce.py).

    NestJS equivalent for the retry pattern:
        async announceWithRetry() {
            for (const delay of [5000, 10000, 30000, 60000]) {
                try { await this.http.post(url, payload); return; }
                catch { await sleep(delay); }
            }
        }
"""

from __future__ import annotations

import asyncio

import httpx

from .state import UnitState


# Backoff schedule in seconds — matches the real Pico's retry pattern
_BACKOFF_SCHEDULE = [5, 10, 30, 60]


async def announce_once(
    state: UnitState,
    server_url: str,
    hub_secret: str = "mushpi-dev-secret",
) -> bool:
    """One-shot POST to the server's announce endpoint.

    Returns True on HTTP 2xx, False otherwise.

    The payload shape must match AnnouncePicoUnitDto on the server:
        - handle (required): unit name
        - ip, port, mac (optional): network identity
        - software_version, micropython_version, board (optional): metadata
        - board_total_mem_byte, board_total_fs_byte, board_cpu_freq_mhz (optional)

    The X-Pico-Secret header must match the server's PICO_ANNOUNCE_SECRET env var.
    """
    url = f"{server_url}/v1/pico-units/announce"

    health = state.get_health_snapshot()
    payload = {
        "handle": state.handle,
        "ip": "127.0.0.1",
        "port": state.port,
        "mac": state._system_info["wifi"]["mac"],
        "software_version": state._system_info["software"]["version"],
        "micropython_version": state._system_info["micropython"]["build"],
        "board": state._system_info["hardware"]["board"],
        "board_total_mem_byte": health["mem"]["total"],
        "board_total_fs_byte": health["fs"]["total"],
        "board_cpu_freq_mhz": state._system_info["hardware"]["cpu"]["freq_mhz"],
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Pico-Secret": hub_secret,
                },
            )
            if 200 <= resp.status_code < 300:
                print(f"  [{state.handle}] Announced successfully to {url}")
                return True
            else:
                print(
                    f"  [{state.handle}] Announce failed: HTTP {resp.status_code}"
                )
                return False
    except Exception as e:
        print(f"  [{state.handle}] Announce error: {e}")
        return False


async def announce_with_retry(
    state: UnitState,
    server_url: str,
    hub_secret: str = "mushpi-dev-secret",
) -> None:
    """Announce with exponential backoff retry.

    Tries immediately, then retries with increasing delays:
    5s → 10s → 30s → 60s (cap).

    This runs as a background task — it doesn't block the HTTP server
    from starting. The real Pico does the same: it starts the HTTP
    server while the announce runs in a separate uasyncio task.

    NestJS equivalent:
        @Cron(CronExpression.EVERY_30_SECONDS)  // not exactly, but similar
        // Or more accurately, an async bootstrap hook:
        async onModuleInit() {
            this.announceWithRetry().catch(() => {});
        }
    """
    for attempt, delay in enumerate(_BACKOFF_SCHEDULE):
        ok = await announce_once(state, server_url, hub_secret)
        if ok:
            return

        if attempt < len(_BACKOFF_SCHEDULE) - 1:
            print(
                f"  [{state.handle}] Retrying announce in {delay}s..."
            )
            await asyncio.sleep(delay)

    print(
        f"  [{state.handle}] All announce attempts failed. "
        f"Server may not know about this unit."
    )
