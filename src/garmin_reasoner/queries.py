"""Read-side queries over stored summaries, parsed from raw JSON at query time."""

import json
import sqlite3
from datetime import date
from typing import Any

# Kept in raw storage but never handed to a model: account identifiers, profile
# images, and exact GPS coordinates (an activity's start point is usually home).
HIDDEN_FIELDS = frozenset(
    {
        "userId",
        "userProfilePk",
        "userProfilePK",
        "userProfileId",
        "displayName",
        "ownerId",
        "ownerDisplayName",
        "ownerFullName",
        "ownerProfileImageUrlSmall",
        "ownerProfileImageUrlMedium",
        "ownerProfileImageUrlLarge",
        "activityLikeDisplayNames",
        "activityLikeProfileImageUrls",
        "startLatitude",
        "startLongitude",
        "endLatitude",
        "endLongitude",
    }
)


def summaries_in_range(
    conn: sqlite3.Connection, summary_type: str, start: date, end: date
) -> list[dict[str, Any]]:
    """Records of one type whose local calendar date is in [start, end], oldest first."""
    return [summary for _, summary in dated_summaries_in_range(conn, summary_type, start, end)]


def dated_summaries_in_range(
    conn: sqlite3.Connection, summary_type: str, start: date, end: date
) -> list[tuple[str, dict[str, Any]]]:
    """Like `summaries_in_range`, paired with each record's calendar date (YYYY-MM-DD)."""
    rows = conn.execute(
        "SELECT calendar_date, raw_json FROM summaries"
        " WHERE type = ? AND calendar_date BETWEEN ? AND ?"
        " ORDER BY calendar_date, start_time, summary_id",
        (summary_type, start.isoformat(), end.isoformat()),
    )
    return [(row["calendar_date"], _without_hidden(json.loads(row["raw_json"]))) for row in rows]


def date_span(conn: sqlite3.Connection, summary_types: list[str]) -> tuple[date, date] | None:
    """First and last calendar date stored for any of these types, or None if there's none."""
    placeholders = ",".join("?" * len(summary_types))
    first, last = conn.execute(
        "SELECT MIN(calendar_date), MAX(calendar_date) FROM summaries"
        f" WHERE type IN ({placeholders})",
        summary_types,
    ).fetchone()
    return (date.fromisoformat(first), date.fromisoformat(last)) if first else None


def data_overview(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT type, COUNT(*) AS count, MIN(calendar_date) AS first_date,"
        " MAX(calendar_date) AS last_date FROM summaries GROUP BY type ORDER BY type"
    )
    return [dict(row) for row in rows]


def _without_hidden(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in summary.items() if k not in HIDDEN_FIELDS}
