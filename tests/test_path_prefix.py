"""Serving under a path prefix (PUBLIC_BASE_URL=https://host/garmin) next to another app.

On the real host, Tailscale Funnel sends only `/garmin/...` and the two prefixed
`/.well-known/...` paths to this app; everything else (`/`) goes to Home Assistant. The
flow below starts from the MCP URL and follows only URLs the server hands out
(discovery, redirects, the login form), asserting each one would reach this app.
"""

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fakes import ADMIN_PASSWORD, API_KEY
from fastapi.testclient import TestClient
from test_mcp import MCP_HEADERS, REDIRECT_URI, VERIFIER, code_challenge

from garmin_reasoner.config import Settings

ORIGIN = "https://example.ts.net"
BASE = f"{ORIGIN}/garmin"
# What deploy/tailscale.sh routes to this app on the shared hostname.
ROUTED_PREFIXES = (
    "/garmin/",
    "/.well-known/oauth-authorization-server/garmin",
    "/.well-known/oauth-protected-resource/garmin/",
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        garmin_api_key=API_KEY,
        admin_password=ADMIN_PASSWORD,
        public_base_url=BASE,
        database_path=tmp_path / "test.db",
        jobs_enabled=False,
        backup_dir=tmp_path / "backups",
    )


def reaches_this_app(url: str) -> bool:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" == ORIGIN and parts.path.startswith(ROUTED_PREFIXES)


def test_connector_flow_stays_inside_routed_paths(client: TestClient) -> None:
    # 1. Unauthenticated MCP request points at the resource metadata.
    challenge = client.post(f"{BASE}/mcp", json={}, headers=MCP_HEADERS)
    assert challenge.status_code == 401
    resource_metadata_url = re.search(
        r'resource_metadata="([^"]+)"', challenge.headers["www-authenticate"]
    ).group(1)
    assert reaches_this_app(resource_metadata_url), resource_metadata_url

    resource = client.get(resource_metadata_url).json()
    assert resource["resource"] == f"{BASE}/mcp"
    [issuer] = resource["authorization_servers"]
    assert issuer == BASE

    # 2. RFC 8414 discovery for a path issuer: insert the well-known segment before the path.
    issuer_path = urlsplit(issuer).path
    metadata_url = f"{ORIGIN}/.well-known/oauth-authorization-server{issuer_path}"
    assert reaches_this_app(metadata_url)
    metadata = client.get(metadata_url).json()
    assert metadata["issuer"] == BASE
    for endpoint in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert reaches_this_app(metadata[endpoint]), metadata[endpoint]

    # 3. Dynamic client registration.
    registered = client.post(
        metadata["registration_endpoint"],
        json={
            "redirect_uris": [REDIRECT_URI],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]

    # 4. Authorize -> our login page (not the site root, which belongs to another app).
    authorize = client.get(
        metadata["authorization_endpoint"],
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
    login_url = authorize.headers["location"]
    assert reaches_this_app(login_url), login_url

    page = client.get(login_url)
    action = re.search(r'<form method="post" action="([^"]+)"', page.text).group(1)
    assert reaches_this_app(action), action
    request_id = parse_qs(urlsplit(login_url).query)["request_id"][0]

    # 5. Approve, get the code back at claude.ai's callback.
    approved = client.post(
        action,
        data={"request_id": request_id, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]

    # 6. Token, then a real MCP call at the prefixed resource URL.
    token = client.post(
        metadata["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert token.status_code == 200, token.text
    initialized = client.post(
        resource["resource"],
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token.json()['access_token']}"},
    )
    assert initialized.status_code == 200, initialized.text


def test_unprefixed_admin_still_works_over_the_tailnet(
    client: TestClient, admin_auth: tuple[str, str]
) -> None:
    # Tailnet-only Serve maps the whole app at "/", so admin paths stay unprefixed.
    response = client.post(
        f"{ORIGIN}/admin/sync", json={"start": "2026-10-02", "end": "2026-10-01"}, auth=admin_auth
    )
    assert response.status_code == 422  # reached the handler (validation), not 404


def test_prefix_is_not_matched_loosely(client: TestClient) -> None:
    assert client.get(f"{ORIGIN}/garminx/healthz").status_code == 404
    assert client.get(f"{BASE}/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("url", ["https://h.ts.net/garmin?x=1", "https://h.ts.net/garmin#f"])
def test_public_url_rejects_query_and_fragment(url: str) -> None:
    with pytest.raises(ValueError, match="query string or fragment"):
        Settings(
            _env_file=None,
            garmin_api_key=API_KEY,
            admin_password=ADMIN_PASSWORD,
            public_base_url=url,
        )
