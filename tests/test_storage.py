import json
import sqlite3

from garmin_reasoner import storage

DAILY = {
    "summaryId": "s1",
    "userId": "u1",
    "calendarDate": "2026-10-24",
    "startTimeInSeconds": 1792800000,
    "steps": 100,
}


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM summaries ORDER BY type, summary_id").fetchall()


def test_upsert_extracts_columns_and_keeps_raw_json(conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "dailies", DAILY, user_id="u1", received_at="t1")

    [row] = rows(conn)
    assert row["type"] == "dailies"
    assert row["summary_id"] == "s1"
    assert row["user_id"] == "u1"
    assert row["calendar_date"] == "2026-10-24"
    assert row["start_time"] == 1792800000
    assert row["received_at"] == "t1"
    assert json.loads(row["raw_json"]) == DAILY


def test_upsert_replaces_resent_summary(conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "dailies", DAILY, user_id="u1", received_at="t1")
    storage.upsert_summary(conn, "dailies", {**DAILY, "steps": 200}, user_id="u1", received_at="t2")

    [row] = rows(conn)
    assert json.loads(row["raw_json"])["steps"] == 200
    assert row["received_at"] == "t2"


def test_same_summary_id_allowed_across_types(conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "dailies", DAILY, user_id="u1", received_at="t")
    storage.upsert_summary(conn, "sleeps", DAILY, user_id="u1", received_at="t")
    assert [r["type"] for r in rows(conn)] == ["dailies", "sleeps"]


def test_upsert_tolerates_missing_optional_fields(conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "activities", {"summaryId": 42}, user_id="u1", received_at="t")

    [row] = rows(conn)
    assert row["summary_id"] == "42"
    assert row["calendar_date"] is None
    assert row["start_time"] is None


def test_known_user_ids(conn: sqlite3.Connection) -> None:
    assert storage.known_user_ids(conn) == set()
    storage.add_user(conn, "u1", "t")
    storage.add_user(conn, "u1", "t")  # idempotent
    assert storage.known_user_ids(conn) == {"u1"}
