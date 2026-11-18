"""Hardening that isn't part of any one feature: what the public internet can reach,
response headers, request size, caps on public endpoints, file permissions."""

import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import ADMIN_PASSWORD, API_KEY, FakeClock
from fastapi.testclient import TestClient
from test_mcp import REDIRECT_URI, VERIFIER, code_challenge, register, start_authorization
from test_path_prefix import BASE, ORIGIN

from garmin_reasoner import backup, db, mcp_auth, mcp_clients
from garmin_reasoner.config import Settings
from garmin_reasoner.main import create_app
from garmin_reasoner.security import MAX_BODY_BYTES


@pytest.fixture
def prefixed_client(tmp_path: Path) -> Iterator[TestClient]:
    """The app as deployed: PUBLIC_BASE_URL has a path, which Funnel forwards publicly."""
    settings = Settings(
        _env_file=None,
        garmin_api_key=API_KEY,
        admin_password=ADMIN_PASSWORD,
        public_base_url=BASE,
        database_path=tmp_path / "test.db",
        jobs_enabled=False,
        backup_dir=tmp_path / "backups",
    )
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        yield client


# --- what Funnel can reach ---


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/admin/sync"),
        ("GET", "/healthz"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("GET", "/"),
        ("GET", "/nonexistent"),
    ],
)
def test_prefixed_non_public_routes_are_not_found(
    prefixed_client: TestClient, method: str, path: str
) -> None:
    response = prefixed_client.request(method, f"{BASE}{path}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "path",
    [
        f"{BASE}/.well-known/oauth-authorization-server",
        f"{ORIGIN}/.well-known/oauth-authorization-server/garmin",
        f"{ORIGIN}/.well-known/oauth-protected-resource/garmin/mcp",
    ],
)
def test_prefixed_discovery_stays_public(prefixed_client: TestClient, path: str) -> None:
    assert prefixed_client.get(path).status_code == 200


def test_unprefixed_routes_still_work_over_the_tailnet(prefixed_client: TestClient) -> None:
    assert prefixed_client.get(f"{ORIGIN}/healthz").json() == {"status": "ok"}


def test_api_docs_are_disabled(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_public_paths() -> None:
    assert mcp_auth.is_public_path("/mcp")
    assert mcp_auth.is_public_path("/.well-known/oauth-protected-resource/garmin/mcp")
    for path in ("/admin/sync", "/healthz", "/docs", "/mcp/../admin/sync", "/mcpx"):
        assert not mcp_auth.is_public_path(path), path


# --- headers ---


def test_responses_carry_security_headers(client: TestClient) -> None:
    headers = client.get("/healthz").headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["strict-transport-security"].startswith("max-age=")
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "server" not in headers


def test_login_page_keeps_its_own_csp(client: TestClient) -> None:
    request_id = start_authorization(client, register(client).json()["client_id"])
    page = client.get("/mcp-login", params={"request_id": request_id})
    csp = page.headers.get_list("content-security-policy")
    assert len(csp) == 1
    assert "form-action" in csp[0]
    assert "Never approve a link someone sent you" in page.text


# --- request size ---


def test_oversized_body_is_refused(client: TestClient) -> None:
    response = client.post("/token", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413


def test_oversized_chunked_body_is_refused(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536

    response = client.post(
        "/mcp-login",
        content=chunks(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413


def test_invalid_content_length_is_refused(client: TestClient) -> None:
    response = client.post("/token", content=b"a=b", headers={"content-length": "abc"})
    assert response.status_code == 400


# --- caps on unauthenticated OAuth endpoints ---


def test_registration_is_capped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_auth, "MAX_CLIENTS", 3)
    for _ in range(3):
        assert register(client).status_code == 201
    refused = register(client)
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_client_metadata"


def test_abandoned_self_registered_clients_are_pruned(
    make_client: Callable[..., TestClient],
    settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_auth, "MAX_CLIENTS", 3)
    clock = FakeClock()
    client = make_client(clock=clock)
    manual = mcp_clients.create_client(settings, "Claude").client_id
    in_use = register(client).json()["client_id"]
    start_authorization(client, in_use)  # holds a pending request
    abandoned = register(client).json()["client_id"]

    clock.now += mcp_auth.ABANDONED_CLIENT_SECONDS + 1
    assert register(client).status_code == 201  # room was made

    ids = {row[0] for row in conn.execute("SELECT client_id FROM mcp_clients")}
    assert manual in ids  # made by hand: never pruned
    assert in_use in ids
    assert abandoned not in ids


def test_pending_authorizations_are_capped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_auth, "MAX_PENDING_REQUESTS", 2)
    client_id = register(client).json()["client_id"]
    start_authorization(client, client_id)
    start_authorization(client, client_id)

    refused = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": code_challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": "s",
        },
        follow_redirects=False,
    )
    assert "/mcp-login" not in refused.headers.get("location", "")
    assert "temporarily_unavailable" in refused.headers.get("location", refused.text)


# --- file permissions ---


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_backups_are_owner_only(settings: Settings, conn: sqlite3.Connection) -> None:
    path = backup.backup_database(
        settings.database_path, settings.backup_dir, 3, datetime(2026, 10, 25, tzinfo=UTC)
    )
    assert mode(path) == 0o600


def test_restrict_permissions_fixes_existing_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    database = data / "garmin.db"
    with closing(sqlite3.connect(database)):
        pass
    database.chmod(0o644)

    db.restrict_permissions(data)

    assert mode(data) == 0o700
    assert mode(database) == 0o600


def test_startup_restricts_the_data_folder(client: TestClient, settings: Settings) -> None:
    assert mode(settings.database_path.parent) == 0o700
    assert mode(settings.database_path) == 0o600
