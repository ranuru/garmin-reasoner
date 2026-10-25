"""Reads and writes against the SQLite schema in `db.py`."""

import json
import sqlite3
from typing import Any

# Single-user service: every row belongs to the owner of the API key.
USER_ID = "self"


def upsert_summary(
    conn: sqlite3.Connection,
    summary_type: str,
    summary_id: str,
    summary: dict[str, Any],
    received_at: str,
    calendar_date: str | None = None,
    start_time: int | None = None,
) -> None:
    """Store one record as raw JSON, replacing any earlier copy of it.

    Re-pulling a day is normal (data keeps arriving as the watch syncs), so the newest
    copy wins. Only the key, date and start time are columns; everything else stays in
    `raw_json` and is parsed at query time.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO summaries (type, summary_id, user_id, calendar_date, start_time,
                                   upload_time, raw_json, received_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT (type, summary_id) DO UPDATE SET
                calendar_date = excluded.calendar_date,
                start_time    = excluded.start_time,
                raw_json      = excluded.raw_json,
                received_at   = excluded.received_at
            """,
            (
                summary_type,
                summary_id,
                USER_ID,
                calendar_date,
                start_time,
                json.dumps(summary, separators=(",", ":")),
                received_at,
            ),
        )


def sync_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT feed, synced_through, last_success_at, last_error, last_error_at FROM sync_state"
    )
    return {row["feed"]: dict(row) for row in rows}


def record_sync_success(conn: sqlite3.Connection, feed: str, synced_through: str, at: str) -> None:
    """Remember that `feed` is complete up to `synced_through` (only ever moves forward)."""
    with conn:
        conn.execute(
            """
            INSERT INTO sync_state (feed, synced_through, last_success_at)
            VALUES (?, ?, ?)
            ON CONFLICT (feed) DO UPDATE SET
                synced_through  = max(COALESCE(synced_through, ''), excluded.synced_through),
                last_success_at = excluded.last_success_at
            """,
            (feed, synced_through, at),
        )


def record_sync_error(conn: sqlite3.Connection, feed: str, error: str, at: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO sync_state (feed, last_error, last_error_at) VALUES (?, ?, ?)
            ON CONFLICT (feed) DO UPDATE SET
                last_error = excluded.last_error, last_error_at = excluded.last_error_at
            """,
            (feed, error, at),
        )
