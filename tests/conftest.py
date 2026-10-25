import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fakes import ADMIN_PASSWORD, API_KEY, PUBLIC_BASE_URL, FakeClock, FakeGarminAPI
from fastapi.testclient import TestClient

from garmin_reasoner import db
from garmin_reasoner.config import Settings
from garmin_reasoner.garmin import GarminClient
from garmin_reasoner.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Explicit values so tests never read a developer's real .env.
    return Settings(
        _env_file=None,
        garmin_api_key=API_KEY,
        admin_password=ADMIN_PASSWORD,
        public_base_url=PUBLIC_BASE_URL,
        database_path=tmp_path / "test.db",
        jobs_enabled=False,
        backup_dir=tmp_path / "backups",
    )


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = db.connect(settings.database_path)
    db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def garmin_api() -> FakeGarminAPI:
    return FakeGarminAPI()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def garmin_client(garmin_api: FakeGarminAPI, settings: Settings, clock: FakeClock) -> GarminClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(garmin_api.handler))
    return GarminClient(
        http, settings.garmin_api_base_url, settings.garmin_api_key, clock, retry_base_delay=0
    )


@pytest.fixture
def make_client(
    settings: Settings, garmin_api: FakeGarminAPI
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(**overrides: object) -> TestClient:
        options: dict[str, object] = {
            "http_client": httpx.AsyncClient(transport=httpx.MockTransport(garmin_api.handler)),
            "retry_base_delay": 0,
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
