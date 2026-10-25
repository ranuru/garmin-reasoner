import json
import sqlite3

from garmin_reasoner import storage

DAILY = {"calendarDate": "2026-10-24", "movement": {"steps": {"value": 100}}}


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM summaries ORDER BY type, summary_id").fetchall()


def test_upsert_stores_columns_and_raw_json(conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "dailies", "2026-10-24", DAILY, "t1", "2026-10-24", 1792800000)

    [row] = rows(conn)
    assert (row["type"], row["summary_id"], row["user_id"]) == ("dailies", "2026-10-24", "self")
    assert row["calendar_date"] == "2026-10-24"
    assert row["start_time"] == 1792800000
    assert row["received_at"] == "t1"
    assert json.loads(row["raw_json"]) == DAILY


def test_upsert_replaces_repulled_record(conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "dailies", "2026-10-24", DAILY, "t1", "2026-10-24")
    newer = {**DAILY, "movement": {"steps": {"value": 200}}}
    storage.upsert_summary(conn, "dailies", "2026-10-24", newer, "t2", "2026-10-24")

    [row] = rows(conn)
    assert json.loads(row["raw_json"]) == newer
    assert row["received_at"] == "t2"


def test_same_id_allowed_across_types(conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "dailies", "2026-10-24", DAILY, "t", "2026-10-24")
    storage.upsert_summary(conn, "sleeps", "2026-10-24", DAILY, "t", "2026-10-24")
    assert [r["type"] for r in rows(conn)] == ["dailies", "sleeps"]


def test_sync_state_only_moves_forward(conn: sqlite3.Connection) -> None:
    assert storage.sync_state(conn) == {}

    storage.record_sync_success(conn, "sleeps", "2026-10-20", "t1")
    storage.record_sync_success(conn, "sleeps", "2026-10-10", "t2")  # older manual re-sync
    storage.record_sync_error(conn, "sleeps", "HTTP 500", "t3")

    assert storage.sync_state(conn) == {
        "sleeps": {
            "feed": "sleeps",
            "synced_through": "2026-10-20",
            "last_success_at": "t2",
            "last_error": "HTTP 500",
            "last_error_at": "t3",
        }
    }
