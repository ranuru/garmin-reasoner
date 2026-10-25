"""Read-side queries over stored summaries, parsed from raw JSON at query time."""

import json
import sqlite3
from datetime import date
from typing import Any

# The user's local calendar date for a summary. Daily-style summaries carry
# `calendarDate`; others (e.g. activities) only have a UTC start time plus the local
# offset, so derive it from those.
LOCAL_DATE = (
    "COALESCE(calendar_date, date(start_time"
    " + COALESCE(json_extract(raw_json, '$.startTimeOffsetInSeconds'), 0), 'unixepoch'))"
)

# Fields that are meaningless or sensitive to hand to a model.
HIDDEN_FIELDS = frozenset({"userId", "userAccessToken"})


def summaries_in_range(
    conn: sqlite3.Connection, summary_type: str, start: date, end: date
) -> list[dict[str, Any]]:
    """Summaries of one type whose local date is in [start, end], oldest first."""
    rows = conn.execute(
        f"SELECT raw_json FROM summaries WHERE type = ? AND {LOCAL_DATE} BETWEEN ? AND ?"
        f" ORDER BY {LOCAL_DATE}, start_time",
        (summary_type, start.isoformat(), end.isoformat()),
    )
    return [_without_hidden(json.loads(row["raw_json"])) for row in rows]


def present_dates(
    conn: sqlite3.Connection, user_id: str, summary_type: str, start: date, end: date
) -> set[date]:
    rows = conn.execute(
        f"SELECT DISTINCT {LOCAL_DATE} AS d FROM summaries"
        f" WHERE user_id = ? AND type = ? AND {LOCAL_DATE} BETWEEN ? AND ?",
        (user_id, summary_type, start.isoformat(), end.isoformat()),
    )
    return {date.fromisoformat(row["d"]) for row in rows}


def data_overview(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT type, COUNT(*) AS count, MIN({LOCAL_DATE}) AS first_date,"
        f" MAX({LOCAL_DATE}) AS last_date FROM summaries GROUP BY type ORDER BY type"
    )
    return [dict(row) for row in rows]


def _without_hidden(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in summary.items() if k not in HIDDEN_FIELDS}
