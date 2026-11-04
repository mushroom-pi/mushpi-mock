"""
Server announcement — POST /v1/pico-units/announce, and a poll-aware watchdog.

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
                try {
                    const resp = await this.http.post(url, payload);
                    return resp.data;  // PicoUnit object with .id
                }
                catch { await sleep(delay); }
            }
        }

    The server responds with the full PicoUnit object (including `id`).
    We parse `resp.json()['id']` to get the database-assigned unit ID,
    which is needed for seeding readings directly into SQLite.

    Beyond the firmware's boot-only announce, `announce_watchdog()` below re-
    announces a unit the hub has stopped polling — a mock-only robustness seam
    for the demo stack, where a server-side stale-`ip` overwrite or a restart
    ordering would otherwise leave a registered unit dark forever with no mDNS
    name to fall back to. See that function's docstring for the three gates that
    keep it from ever re-announcing a healthy unit.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .state import _API_VERSION, UnitState

# Backoff schedule in seconds — matches the real Pico's retry pattern
_BACKOFF_SCHEDULE = [5, 10, 30, 60]

# ── Poll-aware re-announce watchdog (mock-only; the firmware has no counterpart) ──
# The hub's cron polls every unit once a minute (`GET /?force=1`). A unit whose
# announce reached the hub but which the hub then stopped reaching — because the
# server overwrote its `ip` with a stale value, or a restart ordering left the
# row pointing at a port nobody answers — has no way back: the boot announce is
# a one-shot with bounded retries, and the demo network resolves no mDNS name
# for the hub to fall back on. The watchdog below closes that gap by re-
# announcing a unit the hub has demonstrably gone quiet on.
#
# Opt-out: set _WATCHDOG_ENABLED to False. The config layer has no natural
# switch for it (every `MockConfig` key is an address, a path or a roster), and
# a per-run flag would be a new CLI/config surface for a fidelity guard nobody
# configures — so this single constant is the seam, documented here.
_WATCHDOG_ENABLED = True

# The server's cron poll interval, the clock this whole section is measured in.
_POLL_INTERVAL_S = 60

# Idle window: how long a successfully-announced unit may go without a single
# `GET /` before we conclude the hub has lost it. Three poll intervals — one
# missed poll is a hiccup, two is a coincidence, three is a lost unit.
_REANNOUNCE_IDLE_S = 3 * _POLL_INTERVAL_S  # 180 s

# Minimum gap between re-announce attempts for one unit. The hub re-applies the
# active batch's setpoints on every announce, so re-announcing a unit the grower
# is hand-driving would fight those overrides; the clamp bounds how often that
# can happen while the unit stays dark.
_REANNOUNCE_MIN_GAP_S = 5 * _POLL_INTERVAL_S  # 300 s

# How often the loop inspects the stamps. Coarser than the idle window on
# purpose: the window is minutes, so a 10 s resolution costs ~nothing and keeps
# five units in one process quiet most of the time.
_WATCHDOG_CHECK_S = 10


async def announce_once(
    state: UnitState,
    server_url: str,
    hub_secret: str = "mushpi-dev-secret",
    announce_ip: str | None = None,
) -> tuple[bool, int | None]:
    """One-shot POST to the server's announce endpoint.

    Returns (success, pico_unit_id). On HTTP 2xx, parses the response body
    for the unit's database ID. On failure, returns (False, None).

    Single writers of the hub state: this function (and its retry wrapper) is the
    ONLY place `state.hub` is mutated, mirroring the firmware where only
    `announce_then_retry_once` writes `hub` (see mushpi-grow/app/state.py —
    "mutated IN PLACE, single writer: app/announce.py"). `connected` therefore
    means "the hub acknowledged our most recent announce" — never live
    reachability — and GET / surfaces exactly that. It is also the sole writer of
    `state.last_announce_ok_monotonic`, the latch the re-announce watchdog below
    reads (no firmware counterpart — the device never re-announces after boot).

    The payload shape must match AnnouncePicoUnitDto on the server and
    spec/openapi.yaml → components.schemas.AnnouncePayload (canonical docs).
    Key order mirrors the firmware's payload() exactly (mushpi-grow/app/announce.py):
        - handle (required): unit name
        - ip, mac, port (optional): network identity
        - micropython_version, firmware_version, api_version (optional): version
          metadata — firmware_version is the represented _SOFTWARE_VERSION,
          api_version is the Pico↔Server REST contract generation (_API_VERSION)
        - board, board_total_mem_byte, board_total_fs_byte, board_cpu_freq_mhz (optional): board metrics

    The `ip` must be a valid IPv4 address (the server validates it with
    @IsIP('4'); the global ValidationPipe has forbidNonWhitelisted, so invalid
    or unknown keys — e.g. a stale "software_version" — get rejected with 422).
    Falls back to "127.0.0.1" when `announce_ip` is unset, preserving the
    original behavior. Set it to a reachable address (e.g. the Docker bridge
    gateway) when the server runs somewhere that can't loop back to the mock.

    The X-Pico-Secret header must match the server's PICO_ANNOUNCE_SECRET env var.
    """
    url = f"{server_url}/v1/pico-units/announce"

    health = state.get_health_snapshot()
    # Key order mirrors the firmware's payload() in mushpi-grow/app/announce.py
    payload = {
        "handle": state.handle,
        "ip": announce_ip or "127.0.0.1",
        "mac": state._system_info["wifi"]["mac"],
        "port": state.port,
        "micropython_version": state._system_info["micropython"]["build"],
        "firmware_version": state._system_info["software"]["version"],
        "api_version": _API_VERSION,
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
                # Server returns the full PicoUnit object — extract the ID
                body = resp.json()
                unit_id: int | None = body.get("id")
                # Hub ACKED the most recent announce (mirrors the firmware setting
                # hub["connected"] on a 2xx). Mutated in place, never reassigned.
                state.hub["connected"] = True
                # Monotonic latch for the watchdog below: `connected` alone cannot
                # answer "has this unit EVER registered?" because every failed
                # attempt clears it. Stamped here, on the ack, so a later failure
                # does not erase the fact that the hub knows this handle.
                state.last_announce_ok_monotonic = time.monotonic()
                print(f"  [{state.handle}] Announced successfully to {url} "
                      f"(unit_id={unit_id})")
                return True, unit_id
            else:
                # A non-2xx clears the ack flag — same as the firmware clearing
                # `connected` after each failed attempt.
                state.hub["connected"] = False
                print(
                    f"  [{state.handle}] Announce failed: HTTP {resp.status_code}"
                )
                return False, None
    except Exception as e:
        state.hub["connected"] = False
        print(f"  [{state.handle}] Announce error: {e}")
        return False, None


async def announce_with_retry(
    state: UnitState,
    server_url: str,
    hub_secret: str = "mushpi-dev-secret",
    announce_ip: str | None = None,
) -> tuple[bool, int | None]:
    """Announce with exponential backoff retry.

    Tries immediately, then retries with increasing delays:
    5s → 10s → 30s → 60s (cap).

    Returns (success, pico_unit_id). Callers that don't need the ID can
    ignore the second element of the tuple.

    This runs as a background task — it doesn't block the HTTP server
    from starting. The real Pico does the same: it starts the HTTP
    server while the announce runs in a separate uasyncio task.

    NestJS equivalent:
        async onModuleInit() {
            const result = await this.announceWithRetry();
            // result = { ok: boolean, unitId: number | null }
        }

    Hub-state single writer, loop start (firmware parity): `configured` reflects
    whether a hub address exists at all, and `connected` is CLEARED here because
    a fresh announce cycle means the previous ack no longer describes the most
    recent announce. The unit never pings the hub between announcements, so
    between attempts `connected` can only mean "the last attempt was acked".
    """
    # AP provisioning mode never announces — a unit only announces itself to a
    # hub in normal (STA) mode. The mock presents as STA today; the guard keeps
    # the mirrored behaviour honest if AP is ever enabled.
    if state.mode == "ap":
        return False, None

    state.hub["configured"] = bool(server_url)
    state.hub["connected"] = False

    for attempt, delay in enumerate(_BACKOFF_SCHEDULE):
        ok, unit_id = await announce_once(state, server_url, hub_secret, announce_ip)
        if ok:
            return True, unit_id


        if attempt < len(_BACKOFF_SCHEDULE) - 1:
            print(
                f"  [{state.handle}] Retrying announce in {delay}s..."
            )
            await asyncio.sleep(delay)

    print(
        f"  [{state.handle}] All announce attempts failed. "
        f"Server may not know about this unit."
    )
    return False, None


# ── Poll-aware re-announce watchdog ─────────────────────────────────────────


async def announce_watchdog(
    state: UnitState,
    server_url: str,
    hub_secret: str = "mushpi-dev-secret",
    announce_ip: str | None = None,
) -> None:
    """Re-announce a unit the hub has gone quiet on — and never a healthy one.

    The failure this closes: the boot announce is one-shot with a bounded ladder
    (see `_BACKOFF_SCHEDULE`), so a unit that announced fine and was then lost by
    the hub — a stale-`ip` overwrite on the server side, a restart ordering that
    leaves the row pointing at a port nobody answers — stays dark forever. On a
    real LAN the hub's mDNS→IP fallback can still reach a `<handle>.local` unit;
    inside the demo Docker network that name does not resolve, so the only path
    back is for the unit to re-announce and let the hub re-learn its address
    (`mushpi-server` does `Object.assign(unit, dto)` on announce, which refreshes
    `ip` and `mac`).

    Three gates, checked inside the lock immediately before an attempt, so no
    poll that arrived in between can be missed:

      1. **Latch** — `state.last_announce_ok_monotonic` must be set. The hub has
         to have acked at least one announce first. A unit that never registered
         is the boot ladder's job, not this watchdog's: re-announcing forever
         after a boot that failed as designed would contradict the firmware's
         bounded-retry behaviour, which this mock mirrors deliberately.
      2. **Healthy means polled** — `state.poll_gap_s()` below
         `_REANNOUNCE_IDLE_S` and this function stays silent. That is the safety
         property: the hub re-applies the active batch's setpoints on every
         announce, so a spurious re-announce would re-apply setpoints and fight
         the grower's manual overrides. A unit the cron is reading is by
         definition not lost, so it is never re-announced — the poll stamp, not
         `hub.connected`, is the liveness signal (`hub.connected` is an ack of
         the last announce; it says nothing about the hub still reaching us).
      3. **Repeat clamp** — at most one attempt per `_REANNOUNCE_MIN_GAP_S`.
         While a unit stays dark it keeps speaking up on that cadence (which is
         how it recovers the moment the hub comes back), but the setpoint
         re-application can happen no more often than that.

    Concurrency: the check-and-announce section is sequential by construction —
    `main.py` creates exactly ONE task per unit, and only after the boot announce
    ladder has finished, so an attempt can never overlap the boot announce or a
    previous attempt of the same unit. The `asyncio.Lock` below is a second,
    local belt: it serialises gate-check-then-announce within this loop (and
    would serialise any helper that later re-enters THIS lock instance). It is
    created per invocation, so it is not by itself a per-unit guarantee — the
    one-task-per-unit wiring in `main.py` is what carries that.

    Nothing under the lock re-acquires it (`asyncio.Lock` is not reentrant), so
    there is no self-deadlock path: the only await inside the critical section is
    `announce_with_retry` → `announce_once` → httpx.

    The loop does not catch `CancelledError` — `main.py`'s shutdown cancels this
    task and the cancellation must propagate so the task actually ends (the
    lock's `__aexit__` still releases on the way out).

    NestJS equivalent: an `@Interval(10_000)` on a service method that reads
    `lastSeen` and calls the announce hook — except the cron here lives in the
    unit, because the firmware has no watchdog at all. This is a deliberate,
    documented mock-only divergence from the device: a real Pico announces on
    boot and never again until it reboots or reconnects Wi-Fi.

    Args:
        state: The unit whose poll/announce stamps gate the behaviour.
        server_url: Hub base URL; empty means there is nothing to re-announce to.
        hub_secret: `X-Pico-Secret` value, passed straight through.
        announce_ip: The IPv4 to report as this unit's address, passed through.
    """
    if not _WATCHDOG_ENABLED:
        print(
            f"  [{state.handle}] Re-announce watchdog off (_WATCHDOG_ENABLED = False)"
        )
        return

    # No hub address → nothing to re-announce to. AP mode never announces at all.
    # Both are "not applicable", so the task retires instead of spinning forever.
    if not server_url or state.mode == "ap":
        return

    lock = asyncio.Lock()
    last_attempt: float | None = None

    while True:
        await asyncio.sleep(_WATCHDOG_CHECK_S)

        # Belt-and-braces serialisation of gate-check + attempt (see docstring:
        # the per-unit guarantee comes from one task per unit in main.py).
        async with lock:
            # Gate 1 — the hub has never acked this unit; the boot ladder owns it.
            if state.last_announce_ok_monotonic is None:
                continue

            # Gate 2 — being polled is the definition of healthy. Re-read inside
            # the lock so a poll that landed since the check above still suppress.
            idle_s = state.poll_gap_s()
            if idle_s < _REANNOUNCE_IDLE_S:
                continue

            # Gate 3 — repeat clamp, measured from the START of the last attempt.
            if last_attempt is not None:
                since = time.monotonic() - last_attempt
                if since < _REANNOUNCE_MIN_GAP_S:
                    continue

            last_attempt = time.monotonic()
            print(
                f"  [{state.handle}] Re-announce watchdog: hub acked our announce "
                f"but no GET / poll for {idle_s:.0f}s (idle window "
                f"{_REANNOUNCE_IDLE_S}s) — assuming the hub lost our address, "
                f"re-announcing to restore it"
            )
            await announce_with_retry(state, server_url, hub_secret, announce_ip)
