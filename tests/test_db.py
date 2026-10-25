import os
import sqlite3
from pathlib import Path

import pytest

from garmin_reasoner import db, storage, sync

EMPTY_BODY_BATTERY = {
    "charged": None,
    "drained": None,
    "startTimestampGMT": None,
    "endTimestampGMT": None,
    "bodyBatteryValuesArray": [[1736899200001, None], [1736899200002, None]],
    "bodyBatteryValueDescriptorDTOList": None,
}
BODY_BATTERY_CASES = [
    ({**EMPTY_BODY_BATTERY}, False),  # as Garmin sends days before the first watch
    ({**EMPTY_BODY_BATTERY, "bodyBatteryValuesArray": None}, False),
    ({}, False),
    ({**EMPTY_BODY_BATTERY, "charged": 0}, True),
    ({**EMPTY_BODY_BATTERY, "startTimestampGMT": "2026-10-01T00:00:00.0"}, True),
    ({**EMPTY_BODY_BATTERY, "bodyBatteryValuesArray": [[1, None], [2, 45]]}, True),
    ({"charged": 60, "drained": 40, "bodyBatteryValuesArray": [[1, 50], [2, 55]]}, True),
]


@pytest.mark.parametrize("report, has_data", BODY_BATTERY_CASES)
def test_migration_7_drops_the_body_battery_reports_the_sync_skips(
    monkeypatch: pytest.MonkeyPatch, report: dict[str, object], has_data: bool
) -> None:
    report = {"date": "2025-01-15", **report}
    assert sync._has_body_battery_data(report) is has_data

    all_migrations = db.MIGRATIONS
    conn = db.connect(":memory:")
    monkeypatch.setattr(db, "MIGRATIONS", all_migrations[:6])
    db.init_db(conn)
    storage.upsert_summary(conn, "bodyBattery", "2025-01-15", report, "t", "2025-01-15")
    storage.upsert_summary(conn, "stress", "2025-01-15", report, "t", "2025-01-15")

    monkeypatch.setattr(db, "MIGRATIONS", all_migrations)
    db.init_db(conn)

    types = [row["type"] for row in conn.execute("SELECT type FROM summaries ORDER BY type")]
    assert types == (["bodyBattery", "stress"] if has_data else ["stress"])


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


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_unwritable_data_dir_fails_with_fix(tmp_path: Path) -> None:
    locked = tmp_path / "data"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match=r"cannot write to .*data.*sudo chown -R"):
            db.ensure_writable_dir(locked)
    finally:
        locked.chmod(0o700)


def test_missing_data_dir_is_created(tmp_path: Path) -> None:
    db.ensure_writable_dir(tmp_path / "new" / "data")
    assert (tmp_path / "new" / "data").is_dir()
