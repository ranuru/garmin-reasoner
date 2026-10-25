"""Reads and writes against the SQLite schema in `db.py`."""

import json
import sqlite3
from typing import Any


def upsert_summary(
    conn: sqlite3.Connection,
    summary_type: str,
    summary: dict[str, Any],
    user_id: str,
    received_at: str,
) -> None:
    """Store one summary as raw JSON, replacing any earlier copy Garmin sent.

    Only fields that every summary type shares are pulled out into columns; everything
    else stays in `raw_json` and is parsed at query time.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO summaries (type, summary_id, user_id, calendar_date, start_time,
                                   upload_time, raw_json, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (type, summary_id) DO UPDATE SET
                user_id       = excluded.user_id,
                calendar_date = excluded.calendar_date,
                start_time    = excluded.start_time,
                upload_time   = excluded.upload_time,
                raw_json      = excluded.raw_json,
                received_at   = excluded.received_at
            """,
            (
                summary_type,
                str(summary["summaryId"]),
                user_id,
                summary.get("calendarDate"),
                summary.get("startTimeInSeconds"),
                None,  # TODO: confirm in the portal docs which field (if any) carries this
                json.dumps(summary, separators=(",", ":")),
                received_at,
            ),
        )


def add_user(conn: sqlite3.Connection, user_id: str, connected_at: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO users (user_id, connected_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (user_id, connected_at),
        )


def known_user_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["user_id"] for row in conn.execute("SELECT user_id FROM users")}
