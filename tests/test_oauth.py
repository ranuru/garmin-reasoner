import asyncio
import sqlite3
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fakes import REGISTRATION_URL, TOKEN_URL, USER_ID, USER_ID_URL, FakeGarminAPI
from fastapi.testclient import TestClient

from garmin_reasoner import oauth, storage
from garmin_reasoner.config import Settings
from garmin_reasoner.storage import TokenSet

NOW = 1_800_000_000


class FakeClock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def token_response(access: str = "access-1", refresh: str = "refresh-1") -> dict[str, object]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 86400,
        "refresh_token_expires_in": 7776000,
        "token_type": "bearer",
    }


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --- PKCE ---


def test_code_challenge_matches_rfc7636_example() -> None:
    # RFC 7636, appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oauth.code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_code_verifier_is_valid_and_random() -> None:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    verifier = oauth.make_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= allowed
    assert oauth.make_code_verifier() != verifier


# --- /connect ---


def test_connect_requires_admin(client: TestClient) -> None:
    assert client.get("/connect", follow_redirects=False).status_code == 401
    wrong = client.get("/connect", auth=("admin", "nope"), follow_redirects=False)
    assert wrong.status_code == 401


def test_connect_redirects_to_garmin_with_pkce(
    client: TestClient, conn: sqlite3.Connection, settings: Settings, admin_auth: tuple[str, str]
) -> None:
    response = client.get("/connect", auth=admin_auth, follow_redirects=False)

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == settings.garmin_authorize_url
    params = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert params["response_type"] == "code"
    assert params["client_id"] == settings.garmin_client_id
    assert params["redirect_uri"] == settings.redirect_uri
    assert params["code_challenge_method"] == "S256"

    [row] = conn.execute("SELECT state, code_verifier FROM oauth_states").fetchall()
    assert params["state"] == row["state"]
    assert params["code_challenge"] == oauth.code_challenge(row["code_verifier"])


# --- /callback ---


@pytest.fixture
def started_flow(client: TestClient, admin_auth: tuple[str, str]) -> str:
    """Run /connect and return the state Garmin would echo back."""
    response = client.get("/connect", auth=admin_auth, follow_redirects=False)
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def serve_successful_exchange(garmin_api: FakeGarminAPI) -> None:
    garmin_api.serve(TOKEN_URL, token_response(), method="POST")
    garmin_api.serve(USER_ID_URL, {"userId": USER_ID})


def test_callback_stores_user_and_tokens(
    client: TestClient,
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    settings: Settings,
    started_flow: str,
) -> None:
    verifier = conn.execute("SELECT code_verifier FROM oauth_states").fetchone()[0]
    serve_successful_exchange(garmin_api)

    response = client.get("/callback", params={"code": "auth-code", "state": started_flow})

    assert response.status_code == 200
    assert response.json() == {"status": "connected", "user_id": USER_ID}
    assert storage.known_user_ids(conn) == {USER_ID}
    tokens = storage.load_tokens(conn, USER_ID)
    assert tokens is not None
    assert (tokens.access_token, tokens.refresh_token) == ("access-1", "refresh-1")

    [token_request] = garmin_api.requests_to(TOKEN_URL)
    assert form(token_request) == {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "code_verifier": verifier,
        "redirect_uri": settings.redirect_uri,
        "client_id": settings.garmin_client_id,
        "client_secret": settings.garmin_client_secret.get_secret_value(),
    }
    [user_request] = garmin_api.requests_to(USER_ID_URL)
    assert user_request.headers["Authorization"] == "Bearer access-1"


def test_callback_state_is_single_use(
    client: TestClient, garmin_api: FakeGarminAPI, started_flow: str
) -> None:
    serve_successful_exchange(garmin_api)
    params = {"code": "auth-code", "state": started_flow}

    assert client.get("/callback", params=params).status_code == 200
    assert client.get("/callback", params=params).status_code == 400


def test_callback_rejects_expired_state(
    make_client: Callable[..., TestClient],
    garmin_api: FakeGarminAPI,
    admin_auth: tuple[str, str],
) -> None:
    clock = FakeClock()
    client = make_client(clock=clock)
    response = client.get("/connect", auth=admin_auth, follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    serve_successful_exchange(garmin_api)

    clock.now += oauth.STATE_TTL_SECONDS + 1
    response = client.get("/callback", params={"code": "auth-code", "state": state})

    assert response.status_code == 400
    assert garmin_api.requests == []


@pytest.mark.parametrize(
    "params",
    [
        {"code": "auth-code", "state": "never-issued"},
        {"code": "auth-code"},
        {"state": "x"},
        {"error": "access_denied"},
    ],
)
def test_callback_rejects_bad_requests(
    client: TestClient, garmin_api: FakeGarminAPI, params: dict[str, str]
) -> None:
    assert client.get("/callback", params=params).status_code == 400
    assert garmin_api.requests == []


def test_callback_token_exchange_failure(
    client: TestClient, conn: sqlite3.Connection, garmin_api: FakeGarminAPI, started_flow: str
) -> None:
    garmin_api.serve(TOKEN_URL, {"error": "invalid_grant"}, status_code=400, method="POST")

    response = client.get("/callback", params={"code": "bad-code", "state": started_flow})

    assert response.status_code == 502
    assert storage.known_user_ids(conn) == set()


# --- TokenManager ---


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tokens(
    settings: Settings, garmin_api: FakeGarminAPI, clock: FakeClock, known_user: str
) -> oauth.TokenManager:
    http = httpx.AsyncClient(transport=httpx.MockTransport(garmin_api.handler))
    garmin = oauth.GarminClient(settings, http, clock)
    return oauth.TokenManager(settings.database_path, garmin, clock)


def store_tokens(conn: sqlite3.Connection, expires_in: int) -> None:
    storage.save_tokens(conn, USER_ID, TokenSet("old-access", "old-refresh", NOW + expires_in), "t")


@pytest.mark.anyio
async def test_valid_token_is_returned_without_refresh(
    tokens: oauth.TokenManager, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    store_tokens(conn, expires_in=3600)

    assert await tokens.get_access_token(USER_ID) == "old-access"
    assert garmin_api.requests == []


@pytest.mark.anyio
async def test_token_near_expiry_is_refreshed_and_rotated(
    tokens: oauth.TokenManager,
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    settings: Settings,
) -> None:
    store_tokens(conn, expires_in=oauth.REFRESH_MARGIN_SECONDS - 1)
    garmin_api.serve(TOKEN_URL, token_response("new-access", "new-refresh"), method="POST")

    assert await tokens.get_access_token(USER_ID) == "new-access"

    [request] = garmin_api.requests_to(TOKEN_URL)
    assert form(request) == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "client_id": settings.garmin_client_id,
        "client_secret": settings.garmin_client_secret.get_secret_value(),
    }
    stored = storage.load_tokens(conn, USER_ID)
    assert stored == TokenSet("new-access", "new-refresh", NOW + 86400, NOW + 7776000)


@pytest.mark.anyio
async def test_refresh_keeps_old_refresh_token_if_none_returned(
    tokens: oauth.TokenManager, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    store_tokens(conn, expires_in=0)
    garmin_api.serve(TOKEN_URL, {"access_token": "new-access", "expires_in": 60}, method="POST")

    await tokens.get_access_token(USER_ID)

    stored = storage.load_tokens(conn, USER_ID)
    assert stored is not None
    assert stored.refresh_token == "old-refresh"


@pytest.mark.anyio
async def test_concurrent_callers_share_one_refresh(
    tokens: oauth.TokenManager, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    store_tokens(conn, expires_in=0)
    garmin_api.serve(TOKEN_URL, token_response("new-access"), method="POST")

    results = await asyncio.gather(*(tokens.get_access_token(USER_ID) for _ in range(3)))

    assert results == ["new-access"] * 3
    assert len(garmin_api.requests_to(TOKEN_URL)) == 1


@pytest.mark.anyio
async def test_failed_refresh_raises_no_access_token(
    tokens: oauth.TokenManager, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    store_tokens(conn, expires_in=0)
    garmin_api.serve(TOKEN_URL, {"error": "invalid_grant"}, status_code=400, method="POST")

    with pytest.raises(oauth.NoAccessTokenError):
        await tokens.get_access_token(USER_ID)
    stored = storage.load_tokens(conn, USER_ID)
    assert stored is not None
    assert stored.access_token == "old-access"


@pytest.mark.anyio
async def test_missing_tokens_raise_no_access_token(tokens: oauth.TokenManager) -> None:
    with pytest.raises(oauth.NoAccessTokenError):
        await tokens.get_access_token(USER_ID)


def test_token_repr_hides_secrets() -> None:
    text = repr(TokenSet("secret-access", "secret-refresh", NOW))
    assert "secret" not in text


# --- /admin/disconnect ---


def test_disconnect_requires_admin(client: TestClient) -> None:
    assert client.post("/admin/disconnect").status_code == 401


def test_disconnect_deregisters_and_forgets_user(
    client: TestClient,
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    known_user: str,
    admin_auth: tuple[str, str],
) -> None:
    store_tokens(conn, expires_in=10**10)
    storage.upsert_summary(conn, "dailies", {"summaryId": "d1"}, USER_ID, "t")
    garmin_api.serve(REGISTRATION_URL, status_code=204, method="DELETE")

    response = client.post("/admin/disconnect", auth=admin_auth)

    assert response.status_code == 200
    assert response.json() == {"disconnected": [USER_ID]}
    [request] = garmin_api.requests_to(REGISTRATION_URL)
    assert request.headers["Authorization"] == "Bearer old-access"
    assert storage.known_user_ids(conn) == set()
    assert storage.load_tokens(conn, USER_ID) is None
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1


def test_disconnect_keeps_user_if_garmin_fails(
    client: TestClient,
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    known_user: str,
    admin_auth: tuple[str, str],
) -> None:
    store_tokens(conn, expires_in=10**10)
    garmin_api.serve(REGISTRATION_URL, status_code=500, method="DELETE")

    response = client.post("/admin/disconnect", auth=admin_auth)

    assert response.status_code == 502
    assert storage.known_user_ids(conn) == {USER_ID}
