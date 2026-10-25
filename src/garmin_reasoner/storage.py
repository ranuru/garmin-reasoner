"""Reads and writes against the SQLite schema in `db.py`."""

import json
import sqlite3
from dataclasses import dataclass
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


def delete_user(conn: sqlite3.Connection, user_id: str) -> None:
    """Forget a user and their tokens. Their stored summaries are kept."""
    with conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


def known_user_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["user_id"] for row in conn.execute("SELECT user_id FROM users")}


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    access_expires_at: int  # epoch seconds
    refresh_expires_at: int | None = None

    def __repr__(self) -> str:
        return f"TokenSet(access_expires_at={self.access_expires_at}, ...)"


def save_tokens(conn: sqlite3.Connection, user_id: str, tokens: TokenSet, updated_at: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO tokens (user_id, access_token, refresh_token, access_expires_at,
                                refresh_expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token       = excluded.access_token,
                refresh_token      = excluded.refresh_token,
                access_expires_at  = excluded.access_expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                updated_at         = excluded.updated_at
            """,
            (
                user_id,
                tokens.access_token,
                tokens.refresh_token,
                tokens.access_expires_at,
                tokens.refresh_expires_at,
                updated_at,
            ),
        )


def load_tokens(conn: sqlite3.Connection, user_id: str) -> TokenSet | None:
    row = conn.execute(
        "SELECT access_token, refresh_token, access_expires_at, refresh_expires_at"
        " FROM tokens WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return TokenSet(**dict(row)) if row else None


def save_oauth_state(
    conn: sqlite3.Connection, state: str, code_verifier: str, created_at: int
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO oauth_states (state, code_verifier, created_at) VALUES (?, ?, ?)",
            (state, code_verifier, created_at),
        )


def pop_oauth_state(conn: sqlite3.Connection, state: str, not_before: int) -> str | None:
    """Consume a state, returning its code verifier, or None if unknown or too old.

    Also purges every expired state, so abandoned flows don't pile up.
    """
    with conn:
        conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (not_before,))
        rows = conn.execute(
            "DELETE FROM oauth_states WHERE state = ? RETURNING code_verifier", (state,)
        ).fetchall()
    return rows[0]["code_verifier"] if rows else None
