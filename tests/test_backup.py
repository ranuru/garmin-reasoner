import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from garmin_reasoner import backup, jobs, storage
from garmin_reasoner.config import Settings


def test_backup_is_a_readable_copy(settings: Settings, conn: sqlite3.Connection) -> None:
    storage.upsert_summary(conn, "dailies", "d1", {}, "t", "2026-10-24")

    path = backup.backup_database(
        settings.database_path, settings.backup_dir, keep=3, now=datetime(2026, 10, 25, tzinfo=UTC)
    )

    assert path.name == "garmin-20261025T000000Z.db"
    with closing(sqlite3.connect(path)) as copy:
        assert copy.execute("SELECT summary_id FROM summaries").fetchall() == [("d1",)]
    assert not list(settings.backup_dir.glob("*.partial"))


def test_backup_keeps_newest(settings: Settings, conn: sqlite3.Connection) -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(5):
        backup.backup_database(
            settings.database_path, settings.backup_dir, 2, start + timedelta(days=i)
        )

    names = sorted(p.name for p in Path(settings.backup_dir).iterdir())
    assert names == ["garmin-20261004T000000Z.db", "garmin-20261005T000000Z.db"]


@pytest.mark.anyio
async def test_job_keeps_running_after_a_failure() -> None:
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    task = asyncio.create_task(jobs.run_periodically("flaky", flaky, interval=0, initial_delay=0))
    while calls < 3:
        await asyncio.sleep(0)
    task.cancel()

    assert calls >= 3
