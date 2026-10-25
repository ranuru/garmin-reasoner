"""Manually created OAuth clients (for claude.ai's Advanced settings), end to end."""

import base64
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_mcp import REDIRECT_URI, VERIFIER, initialize, start_authorization, submit_login

from garmin_reasoner import mcp_clients
from garmin_reasoner.config import Settings


@pytest.fixture
def manual_client(settings: Settings, conn) -> tuple[str, str]:
    client = mcp_clients.create_client(settings, "Claude")
    assert client.client_secret
    return client.client_id, client.client_secret


def authorization_code(client: TestClient, client_id: str) -> str:
    approved = submit_login(client, start_authorization(client, client_id))
    assert approved.status_code == 303
    return parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]


def token_request(
    client: TestClient, code: str, client_id: str, secret: str, method: str
) -> httpx.Response:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": VERIFIER,
    }
    if method == "post":
        return client.post("/token", data={**form, "client_id": client_id, "client_secret": secret})
    credentials = base64.b64encode(f"{quote(client_id)}:{quote(secret)}".encode()).decode()
    return client.post("/token", data=form, headers={"Authorization": f"Basic {credentials}"})


@pytest.mark.parametrize("method", ["post", "basic"])
def test_manual_client_works_with_either_auth_method(
    client: TestClient, manual_client: tuple[str, str], method: str
) -> None:
    client_id, secret = manual_client

    response = token_request(
        client, authorization_code(client, client_id), client_id, secret, method
    )

    assert response.status_code == 200, response.text
    assert initialize(client, response.json()["access_token"]).status_code == 200


@pytest.mark.parametrize("method", ["post", "basic"])
def test_manual_client_rejects_wrong_secret(
    client: TestClient, manual_client: tuple[str, str], method: str
) -> None:
    client_id, _ = manual_client

    response = token_request(
        client, authorization_code(client, client_id), client_id, "wrong", method
    )

    assert response.status_code == 401


def test_deleting_client_revokes_its_tokens(
    client: TestClient, settings: Settings, manual_client: tuple[str, str]
) -> None:
    client_id, secret = manual_client
    tokens = token_request(client, authorization_code(client, client_id), client_id, secret, "post")
    access_token = tokens.json()["access_token"]

    assert mcp_clients.delete_client(settings, client_id)

    assert initialize(client, access_token).status_code == 401
    assert mcp_clients.list_clients(settings) == []


def test_cli_create_list_delete(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_clients, "get_settings", lambda: settings)

    assert mcp_clients.main(["create", "--name", "Claude"]) == 0
    out = capsys.readouterr().out
    client_id = out.split("OAuth Client ID:")[1].split()[0]
    assert "OAuth Client Secret:" in out

    assert mcp_clients.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert client_id in listed
    assert "secret" not in listed.lower().replace("client_secret_post", "")

    assert mcp_clients.main(["delete", client_id]) == 0
    assert mcp_clients.main(["delete", client_id]) == 1
