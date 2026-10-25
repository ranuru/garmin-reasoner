import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fakes import ACCESS_TOKEN, ADMIN_PASSWORD, PUBLIC_BASE_URL, USER_ID, FakeGarminAPI
from fastapi.testclient import TestClient

from garmin_reasoner import db, storage
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
        admin_password=ADMIN_PASSWORD,
        public_base_url=PUBLIC_BASE_URL,
        jobs_enabled=False,
        backup_dir=tmp_path / "backups",
        database_path=tmp_path / "test.db",
    )


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = db.connect(settings.database_path)
    db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def known_user(conn: sqlite3.Connection) -> str:
    storage.add_user(conn, USER_ID, "2026-10-01T00:00:00+00:00")
    return USER_ID


@pytest.fixture
def garmin_api() -> FakeGarminAPI:
    return FakeGarminAPI()


@pytest.fixture
def make_client(
    settings: Settings, garmin_api: FakeGarminAPI
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    async def fake_token_provider(user_id: str) -> str:
        return ACCESS_TOKEN

    def make(**overrides: object) -> TestClient:
        options: dict[str, object] = {
            "http_client": httpx.AsyncClient(transport=httpx.MockTransport(garmin_api.handler)),
            "token_provider": fake_token_provider,
            "backfill_delay": 0,
        }
        options.update(overrides)
        # The MCP transport only accepts requests addressed to the public host.
        client = TestClient(create_app(settings, **options), base_url=PUBLIC_BASE_URL)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def admin_auth() -> tuple[str, str]:
    return ("admin", ADMIN_PASSWORD)
