import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from garmin_reasoner import db
from garmin_reasoner.config import Settings
from garmin_reasoner.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Explicit values so tests never read a developer's real .env.
    return Settings(
        _env_file=None,
        garmin_client_id="test-client-id",
        garmin_client_secret="test-client-secret",
        redirect_uri="https://example.ts.net/callback",
        database_path=tmp_path / "test.db",
    )


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = db.connect(settings.database_path)
    db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client
