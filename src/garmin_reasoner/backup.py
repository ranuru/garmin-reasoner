"""SQLite backups: consistent online copies, pruned to the newest N.

The database is the only copy of the history (and holds OAuth tokens), so copy the
backup directory off the server too, and treat it as secret.

Run manually with `python -m garmin_reasoner.backup`.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from garmin_reasoner import db

log = logging.getLogger(__name__)

PREFIX = "garmin-"


def backup_database(database_path: Path, backup_dir: Path, keep: int, now: datetime) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{PREFIX}{now.astimezone(UTC):%Y%m%dT%H%M%SZ}.db"
    partial = target.with_suffix(".db.partial")

    # sqlite3's backup API copies a consistent snapshot even while the app is writing.
    with closing(db.connect(database_path)) as source, closing(sqlite3.connect(partial)) as dest:
        source.backup(dest)
    partial.replace(target)  # atomic: a half-written file never looks like a backup

    for old in sorted(backup_dir.glob(f"{PREFIX}*.db"))[:-keep]:
        old.unlink()
    log.info("backed up database to %s", target)
    return target


def main() -> None:
    from garmin_reasoner.config import get_settings

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    backup_database(
        settings.database_path, settings.backup_dir, settings.backup_keep, datetime.now(UTC)
    )


if __name__ == "__main__":
    main()
