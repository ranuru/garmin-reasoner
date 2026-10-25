"""Webhook tests against fake push/ping payloads.

Payload shapes follow Garmin's Health API conventions (body keyed by summary type, each a
list of entries). Verify field names against the partner docs as feeds are enabled.
"""

import json
import sqlite3
from collections.abc import Callable

import pytest
from fakes import ACCESS_TOKEN, USER_ID, FakeGarminAPI
from fastapi.testclient import TestClient

from garmin_reasoner import webhook

pytestmark = pytest.mark.usefixtures("known_user")

CALLBACK_URL = (
    "https://apis.garmin.com/wellness-api/rest/dailies"
    "?uploadStartTimeInSeconds=1792800000&uploadEndTimeInSeconds=1792886400&token=abc"
)


def daily(summary_id: str, **fields: object) -> dict[str, object]:
    return {
        "userId": USER_ID,
        "summaryId": summary_id,
        "calendarDate": "2026-10-24",
        "startTimeInSeconds": 1792800000,
        "steps": 8000,
        **fields,
    }


def ping(summary_type: str = "dailies", url: str = CALLBACK_URL) -> dict[str, object]:
    return {
        summary_type: [
            {
                "userId": USER_ID,
                "uploadStartTimeInSeconds": 1792800000,
                "uploadEndTimeInSeconds": 1792886400,
                "callbackURL": url,
            }
        ]
    }


def stored(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (r["type"], r["summary_id"])
        for r in conn.execute("SELECT type, summary_id FROM summaries ORDER BY 1, 2")
    ]


# --- push ---


def test_push_stores_each_summary(client: TestClient, conn: sqlite3.Connection) -> None:
    body = {
        "dailies": [daily("d1"), daily("d2")],
        "sleeps": [daily("s1", durationInSeconds=28800)],
    }
    response = client.post("/garmin/webhook", json=body)

    assert response.status_code == 200
    assert stored(conn) == [("dailies", "d1"), ("dailies", "d2"), ("sleeps", "s1")]
    raw = conn.execute("SELECT raw_json FROM summaries WHERE summary_id = 's1'").fetchone()[0]
    assert json.loads(raw)["durationInSeconds"] == 28800


def test_push_resend_overwrites(client: TestClient, conn: sqlite3.Connection) -> None:
    client.post("/garmin/webhook", json={"dailies": [daily("d1", steps=1)]})
    client.post("/garmin/webhook", json={"dailies": [daily("d1", steps=2)]})

    [raw] = conn.execute("SELECT raw_json FROM summaries").fetchone()
    assert json.loads(raw)["steps"] == 2


def test_unknown_user_is_dropped(client: TestClient, conn: sqlite3.Connection) -> None:
    body = {"dailies": [daily("d1", userId="someone-else"), daily("d2")]}
    response = client.post("/garmin/webhook", json=body)

    assert response.status_code == 200
    assert stored(conn) == [("dailies", "d2")]


def test_malformed_entries_are_skipped(client: TestClient, conn: sqlite3.Connection) -> None:
    body = {
        "dailies": [daily("d1"), "not-an-object", {"userId": USER_ID}],
        "sleeps": "not-a-list",
        "deregistrations": [{"userId": USER_ID}],
    }
    response = client.post("/garmin/webhook", json=body)

    assert response.status_code == 200
    assert stored(conn) == [("dailies", "d1")]


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"string"'])
def test_rejects_non_object_body(client: TestClient, body: bytes) -> None:
    response = client.post(
        "/garmin/webhook", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


# --- ping ---


def test_ping_fetches_callback_with_bearer_token(
    client: TestClient, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve(CALLBACK_URL, [daily("d1"), daily("d2")])

    response = client.post("/garmin/webhook", json=ping())

    assert response.status_code == 200
    assert stored(conn) == [("dailies", "d1"), ("dailies", "d2")]
    [request] = garmin_api.requests
    assert str(request.url) == CALLBACK_URL
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/wellness-api/rest/dailies",
        "http://apis.garmin.com/wellness-api/rest/dailies",
        "https://apis.garmin.com.evil.example.com/x",
        "https://apis.garmin.com:8443/x",
        "https://user@evil.example.com/?apis.garmin.com",
    ],
)
def test_ping_refuses_non_garmin_callback(
    client: TestClient, conn: sqlite3.Connection, garmin_api: FakeGarminAPI, url: str
) -> None:
    response = client.post("/garmin/webhook", json=ping(url=url))

    assert response.status_code == 200
    assert garmin_api.requests == []
    assert stored(conn) == []


def test_ping_for_unknown_user_is_not_fetched(
    client: TestClient, garmin_api: FakeGarminAPI
) -> None:
    body = ping()
    body["dailies"][0]["userId"] = "someone-else"
    client.post("/garmin/webhook", json=body)

    assert garmin_api.requests == []


@pytest.mark.parametrize("status_code, payload", [(500, {}), (200, {"not": "a list"})])
def test_ping_callback_failure_is_survivable(
    client: TestClient,
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    status_code: int,
    payload: object,
) -> None:
    garmin_api.serve(CALLBACK_URL, payload, status_code=status_code)
    body = {**ping(), "sleeps": [daily("s1")]}

    response = client.post("/garmin/webhook", json=body)

    assert response.status_code == 200
    assert stored(conn) == [("sleeps", "s1")]


def test_ping_without_token_is_logged_not_fatal(
    make_client: Callable[..., TestClient],
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = make_client(token_provider=webhook.no_access_token)

    response = client.post("/garmin/webhook", json=ping())

    assert response.status_code == 200
    assert garmin_api.requests == []
    assert "failed to fetch 'dailies' ping callback" in caplog.text
