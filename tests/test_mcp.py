"""End-to-end tests of the MCP endpoint as claude.ai's custom connector would use it:
register -> authorize -> log in -> token -> call tools."""

import base64
import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fakes import ADMIN_PASSWORD, PUBLIC_BASE_URL, FakeClock
from fastapi.testclient import TestClient

from garmin_reasoner import storage

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


VERIFIER = secrets.token_urlsafe(64)
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def register(client: TestClient, redirect_uri: str = REDIRECT_URI) -> httpx.Response:
    return client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )


def start_authorization(client: TestClient, client_id: str) -> str:
    """GET /authorize and return the login page's request_id."""
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": code_challenge(VERIFIER),
            "code_challenge_method": "S256",
            "state": "claude-state",
            "scope": "read",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{PUBLIC_BASE_URL}/mcp-login"
    return parse_qs(location.query)["request_id"][0]


def submit_login(
    client: TestClient, request_id: str, password: str = ADMIN_PASSWORD
) -> httpx.Response:
    return client.post(
        "/mcp-login",
        data={"request_id": request_id, "password": password},
        follow_redirects=False,
    )


def exchange_code(client: TestClient, client_id: str, code: str) -> httpx.Response:
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )


def connect_claude(client: TestClient) -> tuple[str, dict[str, Any]]:
    """Run the whole connector flow; return (client_id, token response)."""
    client_id = register(client).json()["client_id"]
    approved = submit_login(client, start_authorization(client, client_id))
    assert approved.status_code == 303
    redirect = urlsplit(approved.headers["location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == REDIRECT_URI
    params = parse_qs(redirect.query)
    assert params["state"] == ["claude-state"]
    tokens = exchange_code(client, client_id, params["code"][0])
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def rpc(
    client: TestClient, token: str, method: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )


def initialize(client: TestClient, token: str) -> httpx.Response:
    return rpc(
        client,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )


def call_tool(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


# --- discovery ---


def test_metadata_points_at_public_url(client: TestClient) -> None:
    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == PUBLIC_BASE_URL
    assert server["authorization_endpoint"] == f"{PUBLIC_BASE_URL}/authorize"
    assert server["token_endpoint"] == f"{PUBLIC_BASE_URL}/token"
    assert server["registration_endpoint"] == f"{PUBLIC_BASE_URL}/register"
    assert "S256" in server["code_challenge_methods_supported"]

    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == f"{PUBLIC_BASE_URL}/mcp"
    assert resource["authorization_servers"] == [PUBLIC_BASE_URL]


def test_mcp_requires_bearer_token(client: TestClient) -> None:
    response = initialize(client, "not-a-real-token")
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]

    response = client.post("/mcp", json={}, headers=MCP_HEADERS)
    assert response.status_code == 401


# --- the flow ---


def test_full_connector_flow_and_tool_call(client: TestClient, conn: sqlite3.Connection) -> None:
    storage.upsert_summary(
        conn,
        "dailies",
        "2026-10-20",
        {"calendarDate": "2026-10-20", "userProfilePk": 1, "steps": 9000},
        "t",
        "2026-10-20",
    )
    storage.record_sync_success(conn, "dailies", "2026-10-21", "2026-10-21T10:00:00+00:00")
    _, tokens = connect_claude(client)
    token = tokens["access_token"]
    assert tokens["scope"] == "read"
    assert tokens["refresh_token"]

    assert initialize(client, token).status_code == 200

    listed = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listed} == {
        "list_available_data",
        "get_period_stats",
        "get_sleep_summary",
        "get_daily_overview",
        "get_activity_list",
        "get_daily_summaries",
        "get_sleep",
        "get_hrv",
        "get_activities",
        "get_stress",
        "get_body_battery",
        "get_weight",
    }
    assert all(t["annotations"]["readOnlyHint"] for t in listed)

    result = call_tool(
        client, token, "get_daily_summaries", {"start": "2026-10-19", "end": "2026-10-21"}
    )
    assert not result.get("isError")
    content = result["structuredContent"]
    assert content["count"] == 1
    assert content["summaries"] == [{"calendarDate": "2026-10-20", "steps": 9000}]

    overview = call_tool(client, token, "list_available_data", {})["structuredContent"]
    assert overview["types"][0]["type"] == "dailies"
    assert overview["sync"]["dailies"]["synced_through"] == "2026-10-21"
    assert overview["garmin_api_key_expires"] is None


def test_tool_rejects_oversized_range(client: TestClient) -> None:
    _, tokens = connect_claude(client)
    result = call_tool(
        client, tokens["access_token"], "get_sleep", {"start": "2026-01-01", "end": "2026-12-31"}
    )
    assert result["isError"]


def test_summary_tool_returns_compact_rows(client: TestClient, conn: sqlite3.Connection) -> None:
    storage.upsert_summary(
        conn,
        "dailies",
        "2026-10-20",
        {"calendarDate": "2026-10-20", "userProfilePk": 1, "movement": {"steps": {"value": 9000}}},
        "t",
        "2026-10-20",
    )
    _, tokens = connect_claude(client)

    result = call_tool(
        client,
        tokens["access_token"],
        "get_daily_overview",
        {"start": "2026-10-20", "end": "2026-10-21"},
    )

    assert not result.get("isError")
    assert "structuredContent" not in result or result["structuredContent"] is None
    [content] = result["content"]
    assert "\n  " not in content["text"]  # not pretty-printed
    body = json.loads(content["text"])
    assert body["missing"] == ["2026-10-21"]
    [row] = body["rows"]
    assert row["steps"] == 9000
    assert row["resting_hr"] is None


@pytest.mark.parametrize(
    "tool, arguments, ok",
    [
        ("get_sleep_summary", {"start": "2026-07-25", "end": "2026-10-25"}, True),  # 93 days
        ("get_sleep_summary", {"start": "2026-07-24", "end": "2026-10-25"}, False),
        ("get_period_stats", {"start": "2025-10-25", "end": "2026-10-25"}, True),  # 366 days
        ("get_period_stats", {"start": "2025-10-24", "end": "2026-10-25"}, False),
        (
            "get_period_stats",
            {"start": "2000-01-01", "end": "2026-10-25", "group_by": "month"},
            True,
        ),
        (
            "get_period_stats",
            {"start": "2000-01-01", "end": "2026-10-25", "group_by": "none"},
            True,
        ),
        (
            "get_period_stats",
            {"start": "2026-10-01", "end": "2026-10-25", "group_by": "day"},
            False,
        ),
        ("get_activity_list", {"start": "2026-10-25", "end": "2026-10-24"}, False),
    ],
)
def test_summary_tool_ranges(
    client: TestClient, tool: str, arguments: dict[str, Any], ok: bool
) -> None:
    _, tokens = connect_claude(client)

    result = call_tool(client, tokens["access_token"], tool, arguments)

    assert bool(result.get("isError")) is not ok


# --- policy ---


def test_registration_rejects_unknown_redirect_uri(client: TestClient) -> None:
    response = register(client, "https://attacker.example.com/callback")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_login_page_names_client_and_redirect(client: TestClient) -> None:
    client_id = register(client).json()["client_id"]
    request_id = start_authorization(client, client_id)

    page = client.get("/mcp-login", params={"request_id": request_id})

    assert page.status_code == 200
    assert "Claude" in page.text
    assert REDIRECT_URI in page.text
    assert page.headers["x-frame-options"] == "DENY"


def test_wrong_password_issues_no_code(client: TestClient) -> None:
    client_id = register(client).json()["client_id"]
    request_id = start_authorization(client, client_id)

    response = submit_login(client, request_id, password="wrong-password-123")

    assert response.status_code == 401
    assert "location" not in response.headers
    # The request survives a typo.
    assert submit_login(client, request_id).status_code == 303


def test_failed_logins_are_throttled(client: TestClient) -> None:
    client_id = register(client).json()["client_id"]
    request_id = start_authorization(client, client_id)
    for _ in range(10):
        assert submit_login(client, request_id, password="guess").status_code == 401

    # Even the right password is refused while throttled.
    assert submit_login(client, request_id).status_code == 429


def test_login_request_is_single_use_and_expires(make_client: Callable[..., TestClient]) -> None:
    clock = FakeClock()
    client = make_client(clock=clock)
    client_id = register(client).json()["client_id"]

    request_id = start_authorization(client, client_id)
    assert submit_login(client, request_id).status_code == 303
    assert submit_login(client, request_id).status_code == 400

    request_id = start_authorization(client, client_id)
    clock.now += 601
    assert submit_login(client, request_id).status_code == 400


def test_authorization_code_is_single_use(client: TestClient) -> None:
    client_id = register(client).json()["client_id"]
    approved = submit_login(client, start_authorization(client, client_id))
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]

    assert exchange_code(client, client_id, code).status_code == 200
    assert exchange_code(client, client_id, code).status_code == 400


def test_code_requires_matching_verifier(client: TestClient) -> None:
    client_id = register(client).json()["client_id"]
    approved = submit_login(client, start_authorization(client, client_id))
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]

    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": secrets.token_urlsafe(64),
        },
    )
    assert response.status_code == 400


def test_refresh_rotates_tokens(client: TestClient) -> None:
    client_id, tokens = connect_claude(client)

    def refresh(token: str) -> httpx.Response:
        return client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id},
        )

    rotated = refresh(tokens["refresh_token"])
    assert rotated.status_code == 200
    new = rotated.json()
    assert new["access_token"] != tokens["access_token"]

    # Old refresh token is spent, and the old access token stopped working with it.
    assert refresh(tokens["refresh_token"]).status_code == 400
    assert initialize(client, tokens["access_token"]).status_code == 401
    assert initialize(client, new["access_token"]).status_code == 200


def test_access_token_expires(make_client: Callable[..., TestClient]) -> None:
    clock = FakeClock()
    client = make_client(clock=clock)
    _, tokens = connect_claude(client)
    assert initialize(client, tokens["access_token"]).status_code == 200

    clock.now += 3601
    assert initialize(client, tokens["access_token"]).status_code == 401


def test_tokens_are_stored_hashed(client: TestClient, conn: sqlite3.Connection) -> None:
    _, tokens = connect_claude(client)
    dump = "\n".join(conn.iterdump())
    assert tokens["access_token"] not in dump
    assert tokens["refresh_token"] not in dump


@pytest.mark.parametrize("host", ["evil.example.com", "localhost:8000"])
def test_mcp_rejects_foreign_host_header(client: TestClient, host: str) -> None:
    _, tokens = connect_claude(client)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}", "Host": host},
    )
    assert response.status_code in (400, 421)


@pytest.mark.parametrize(
    "public_base_url, expected",
    [
        ("https://example.ts.net", ["example.ts.net"]),
        ("https://example.ts.net:10000", ["example.ts.net", "example.ts.net:10000"]),
    ],
)
def test_allowed_hosts(public_base_url: str, expected: list[str]) -> None:
    from garmin_reasoner.mcp_server import allowed_hosts

    assert allowed_hosts(public_base_url) == expected


def test_login_page_csp_allows_redirect_to_client(client: TestClient) -> None:
    """Browsers apply form-action to the redirect after Approve; 'self' alone blocks it."""
    client_id = register(client).json()["client_id"]
    page = client.get("/mcp-login", params={"request_id": start_authorization(client, client_id)})

    csp = page.headers["content-security-policy"]
    form_action = next(d for d in csp.split(";") if d.strip().startswith("form-action")).split()
    assert "'self'" in form_action
    assert "https://claude.ai" in form_action
    assert "https://claude.com" in form_action
    # Nothing broader than the client origins.
    assert "*" not in csp and "https:" not in form_action
