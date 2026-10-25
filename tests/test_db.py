import sqlite3

import pytest

from garmin_reasoner import db


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


def test_raw_json_is_required(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO summaries (type, summary_id, user_id, raw_json, received_at)"
            " VALUES ('dailies', 's1', 'u1', NULL, 't')"
        )


def test_failed_migration_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(":memory:")
    monkeypatch.setattr(db, "MIGRATIONS", ["CREATE TABLE a (x); CREATE TABLE a (x);"])

    with pytest.raises(sqlite3.OperationalError):
        db.init_db(conn)

    assert db.schema_version(conn) == 0
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert tables == []
