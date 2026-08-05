"""
Backdated seed data generator — bulk-inserts synthetic readings into SQLite.

NestJS mapping:
    This has no direct NestJS equivalent — it's a development/demo utility.
    In a TypeScript CLI tool you might use a TypeORM QueryBuilder to bulk-insert
    rows. Here we use Python's stdlib sqlite3 module (no extra dependencies).

The seed generator reuses `compute_hysteresis_tick()` from simulation.py so
the historical data has the same sawtooth oscillation pattern as the live
simulation. This ensures continuity: when the live simulation starts after
seeding, it continues from the final seed values.

Each reading row matches the server's `readings` table schema exactly
(17 columns), so the data is indistinguishable from real cron-polled readings.
"""

from __future__ import annotations

import datetime
import random
import sqlite3

from .simulation import DHT11_HUM_MAX, DHT11_HUM_MIN, DHT11_TEMP_MAX, DHT11_TEMP_MIN, compute_hysteresis_tick
from .state import UnitState

# ── Board metric simulation ranges ─────────────────────────────────────────
# These mimic the real Pico's resource usage. The values are approximate —
# good enough for dashboard demos and chart rendering.
_BOARD_TEMP_BASE = 30.0       # °C — Pico's onboard temp sensor hovers around here
_BOARD_TEMP_DRIFT = 2.0       # ±2°C random walk range
_BOARD_USED_MEM = 262144      # ~256 KB (half of 512 KB total RAM)
_BOARD_USED_FS = 524288       # ~512 KB of 2 MB filesystem
_RESPONSE_MS = 5              # Mock response time (the real Pico responds in ~5-15ms)


def seed_readings(
    pico_unit_id: int,
    db_path: str,
    days: int,
    state: UnitState,
) -> int:
    """Generate and insert N days of synthetic readings into the server's SQLite DB.

    This fast-forwards the hysteresis simulation to produce realistic historical
    data at 60-second intervals (matching the server's cron poll frequency).

    The function:
        1. Walks through `days * 1440` ticks (1440 minutes per day)
        2. Runs compute_hysteresis_tick() for each tick
        3. Simulates board metrics (uptime, temp, memory, filesystem)
        4. Bulk INSERTs all rows in a single transaction
        5. Updates the live state so the simulation continues from the final values

    Args:
        pico_unit_id: The server-assigned ID for this Pico unit (from announce response).
        db_path: Path to the server's SQLite database file.
        days: Number of days of historical data to generate.
        state: The unit's live state — updated with final seed values.

    Returns:
        Number of rows inserted. Returns 0 on error (DB not found, etc.).

    NestJS equivalent:
        const qb = dataSource.createQueryBuilder()
            .insert()
            .into(Readings)
            .values(readings)
            .execute();
    """
    total_readings = days * 1440  # 1440 minutes per day (60s intervals)
    if total_readings <= 0:
        return 0

    # ── Initialize simulation state ────────────────────────────────────
    # Start from the current state values so seeding is continuous with
    # whatever the unit already has (or the initial defaults).
    temp = float(state._sensors["temperature"])
    humidity = float(state._sensors["humidity"])
    outputs = dict(state._outputs)
    setpoints = dict(state.setpoints)
    control_enabled = state.control_enabled
    hyst_temp = state.control_config["hyst_temp"]
    hyst_hum = state.control_config["hyst_hum"]

    # Board metrics simulation state
    board_temp = _BOARD_TEMP_BASE
    board_uptime = 0  # seconds — increments by 60 each tick

    # ── Generate all readings ──────────────────────────────────────────
    now = datetime.datetime.now()  # noqa: DTZ005 — naive local time is fine for seeding
    start_ts = now - datetime.timedelta(days=days)

    rows: list[tuple] = []

    for i in range(total_readings):
        ts = start_ts + datetime.timedelta(seconds=i * 60)

        # Run hysteresis tick (same algorithm as the live simulation)
        temp, humidity, outputs = compute_hysteresis_tick(
            temp=temp,
            humidity=humidity,
            outputs=outputs,
            setpoints=setpoints,
            control_enabled=control_enabled,
            hyst_temp=hyst_temp,
            hyst_hum=hyst_hum,
        )

        # Simulate board temperature: random walk around 30°C
        board_temp += random.uniform(-0.3, 0.3)
        board_temp = max(
            _BOARD_TEMP_BASE - _BOARD_TEMP_DRIFT,
            min(_BOARD_TEMP_BASE + _BOARD_TEMP_DRIFT, board_temp),
        )

        board_uptime += 60

        # Round sensor values to integers (DHT11 returns integers)
        temp_int = int(round(temp))
        hum_int = int(round(humidity))

        # Build the 17-column row matching readings.entity.ts schema:
        # (ts, temperature, humidity, last_sensor_err, fan_on, humidifier_on,
        #  heater_on, control_loop_enabled, temperature_set, humidity_set,
        #  board_uptime_s, board_temp, board_used_mem, board_used_fs,
        #  time_to_response_ms, pico_unit_id)
        rows.append((
            ts.strftime('%Y-%m-%d %H:%M:%S'),  # ts (DATETIME) — matches server cron format
            temp_int,                        # temperature (FLOAT)
            hum_int,                         # humidity (INTEGER)
            None,                            # last_sensor_err (TEXT)
            int(outputs["fan"]),             # fan_on (BOOLEAN)
            int(outputs["humidifier"]),      # humidifier_on (BOOLEAN)
            int(outputs["heater"]),          # heater_on (BOOLEAN)
            int(control_enabled),            # control_loop_enabled (BOOLEAN)
            setpoints["temperature"],        # temperature_set (INTEGER)
            setpoints["humidity"],           # humidity_set (INTEGER)
            board_uptime,                    # board_uptime_s (INTEGER)
            round(board_temp, 1),            # board_temp (FLOAT)
            _BOARD_USED_MEM,                 # board_used_mem (INTEGER)
            _BOARD_USED_FS,                  # board_used_fs (INTEGER)
            _RESPONSE_MS,                    # time_to_response_ms (INTEGER)
            pico_unit_id,                    # pico_unit_id (INTEGER FK)
        ))

    # ── Bulk INSERT into SQLite ────────────────────────────────────────
    # Using a single transaction for performance — 1440 rows/day × 14 days
    # = ~20K rows. Without a transaction, each INSERT would be a separate
    # disk sync (~10ms each = 200 seconds). With a transaction: <1 second.
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("BEGIN TRANSACTION")
        cursor.executemany(
            """
            INSERT INTO readings (
                ts, temperature, humidity, last_sensor_err,
                fan_on, humidifier_on, heater_on, control_loop_enabled,
                temperature_set, humidity_set,
                board_uptime_s, board_temp, board_used_mem, board_used_fs,
                time_to_response_ms, pico_unit_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"  [{state.handle}] Seed DB error: {e}")
        print(f"  [{state.handle}] Is the server DB at '{db_path}'? "
              f"Run the server first to create it.")
        return 0

    # ── Update live state to final seed values ─────────────────────────
    # The live simulation continues from where seeding left off, so there's
    # no discontinuity in the sensor readings.
    state._sensors["temperature"] = int(round(temp))
    state._sensors["humidity"] = int(round(humidity))
    state._outputs.update(outputs)

    return len(rows)
