import sqlite3

import pytest

from garmin_reasoner import db

UPSERT = """
    INSERT INTO summaries
        (type, summary_id, user_id, calendar_date, start_time, upload_time, raw_json, received_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (type, summary_id) DO UPDATE SET
        raw_json = excluded.raw_json, received_at = excluded.received_at
"""


def test_init_db_sets_schema_version(conn: sqlite3.Connection) -> None:
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_init_db_is_idempotent(conn: sqlite3.Connection) -> None:
    db.init_db(conn)
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_summaries_table_has_expected_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(summaries)")}
    assert columns == {
        "type",
        "summary_id",
        "user_id",
        "calendar_date",
        "start_time",
        "upload_time",
        "raw_json",
        "received_at",
    }


def test_upsert_replaces_resent_summary(conn: sqlite3.Connection) -> None:
    row = ["dailies", "s1", "u1", "2026-10-24", 1, 2, '{"steps": 100}', "t1"]
    conn.execute(UPSERT, row)
    conn.execute(UPSERT, row[:6] + ['{"steps": 200}', "t2"])

    rows = conn.execute("SELECT raw_json, received_at FROM summaries").fetchall()
    assert [tuple(r) for r in rows] == [('{"steps": 200}', "t2")]


def test_same_summary_id_allowed_across_types(conn: sqlite3.Connection) -> None:
    conn.execute(UPSERT, ["dailies", "s1", "u1", None, None, None, "{}", "t"])
    conn.execute(UPSERT, ["sleeps", "s1", "u1", None, None, None, "{}", "t"])
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 2


def test_raw_json_is_required(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(UPSERT, ["dailies", "s1", "u1", None, None, None, None, "t"])


def test_failed_migration_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(":memory:")
    monkeypatch.setattr(db, "MIGRATIONS", ["CREATE TABLE a (x); CREATE TABLE a (x);"])

    with pytest.raises(sqlite3.OperationalError):
        db.init_db(conn)

    assert db.schema_version(conn) == 0
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert tables == []
