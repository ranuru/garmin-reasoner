"""SQLite connection handling and schema migrations.

Migrations are an ordered list of SQL scripts. `PRAGMA user_version` records how many
have been applied, so `init_db` is safe to call on every startup.
"""

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: raw summaries. Garmin can resend a summary, so writes upsert on (type, summary_id).
    # Summary IDs are only documented as unique per summary type, hence the composite key.
    """
    CREATE TABLE summaries (
        type          TEXT    NOT NULL,
        summary_id    TEXT    NOT NULL,
        user_id       TEXT    NOT NULL,
        calendar_date TEXT,              -- YYYY-MM-DD, when the summary has one
        start_time    INTEGER,           -- epoch seconds (startTimeInSeconds)
        upload_time   INTEGER,           -- epoch seconds, when Garmin received it
        raw_json      TEXT    NOT NULL,  -- the summary exactly as Garmin sent it
        received_at   TEXT    NOT NULL,  -- ISO 8601 UTC, when we stored it
        PRIMARY KEY (type, summary_id)
    );
    CREATE INDEX idx_summaries_type_date ON summaries (type, calendar_date);
    CREATE INDEX idx_summaries_type_start ON summaries (type, start_time);
    CREATE INDEX idx_summaries_user ON summaries (user_id);
    """,
    # 2: Garmin users we accept data for. Webhook entries for any other userId are dropped.
    """
    CREATE TABLE users (
        user_id      TEXT PRIMARY KEY,  -- Garmin API user ID
        connected_at TEXT NOT NULL      -- ISO 8601 UTC
    );
    """,
    # 3: OAuth. Tokens go away with their user; states are single-use PKCE verifiers
    # for authorization flows that haven't reached /callback yet.
    """
    CREATE TABLE tokens (
        user_id            TEXT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
        access_token       TEXT    NOT NULL,
        refresh_token      TEXT    NOT NULL,
        access_expires_at  INTEGER NOT NULL,  -- epoch seconds
        refresh_expires_at INTEGER,           -- epoch seconds, if Garmin reports it
        updated_at         TEXT    NOT NULL   -- ISO 8601 UTC
    );
    CREATE TABLE oauth_states (
        state         TEXT    PRIMARY KEY,
        code_verifier TEXT    NOT NULL,
        created_at    INTEGER NOT NULL  -- epoch seconds
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas this app relies on."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(conn: sqlite3.Connection) -> None:
    """Apply any migrations that haven't been applied yet, each in its own transaction."""
    current = schema_version(conn)
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        # executescript() commits any pending transaction first, so BEGIN/COMMIT here
        # makes each migration and its version bump atomic. On error it stops mid-script
        # and leaves the transaction open, so roll back explicitly.
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
