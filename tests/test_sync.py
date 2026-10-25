"""Sync tests against fake Personal API responses shaped like docs/garmin-personal-api.json."""

import json
import sqlite3
from datetime import date

import httpx
import pytest
from fakes import FakeClock, FakeGarminAPI
from fastapi.testclient import TestClient

from garmin_reasoner import storage, sync
from garmin_reasoner.config import Settings
from garmin_reasoner.garmin import GarminClient

OCT1, OCT2 = date(2026, 10, 1), date(2026, 10, 2)


@pytest.fixture
def ctx(
    settings: Settings, garmin_client: GarminClient, clock: FakeClock, conn
) -> sync.SyncContext:
    return sync.SyncContext(settings.database_path, garmin_client, clock)


def stored(conn: sqlite3.Connection, summary_type: str) -> list[tuple[str, str, int | None]]:
    return [
        (r["summary_id"], r["calendar_date"], r["start_time"])
        for r in conn.execute(
            "SELECT * FROM summaries WHERE type = ? ORDER BY summary_id", (summary_type,)
        )
    ]


# --- feeds: response shape and record key ---


@pytest.mark.anyio
@pytest.mark.parametrize(
    "feed, path, body, expected",
    [
        (
            "dailies",
            "/usersummary-service/v2/dailysummaries",
            {"data": [{"calendarDate": "2026-10-01", "movement": {}}]},
            [("2026-10-01", "2026-10-01", None)],
        ),
        (
            "sleeps",
            "/sleep-service/sleep/dailySleepSummariesByDate",
            [{"calendarDate": "2026-10-01", "sleepTimeSeconds": 27000}],
            [("2026-10-01", "2026-10-01", None)],
        ),
        (
            "hrv",
            "/hrv-service/hrv/daily/2026-10-01/2026-10-02",
            {
                "hrvSummaries": [{"calendarDate": "2026-10-01", "lastNightAvg": 48}],
                "userProfilePk": 1,
            },
            [("2026-10-01", "2026-10-01", None)],
        ),
        (
            "stress",
            "/usersummary-service/stats/stress/daily/2026-10-01/2026-10-02",
            [{"calendarDate": "2026-10-02", "values": {"overallStressLevel": 31}}],
            [("2026-10-02", "2026-10-02", None)],
        ),
        (
            "bodyBattery",
            "/wellness-service/wellness/bodyBattery/reports/daily",
            [{"date": "2026-10-01", "charged": 60}],
            [("2026-10-01", "2026-10-01", None)],
        ),
        (
            "weight",
            "/weight-service/weight/range/2026-10-01/2026-10-02",
            {
                "dailyWeightSummaries": [
                    {
                        "summaryDate": "2026-10-01",
                        "allWeightMetrics": [
                            {
                                "samplePk": 11,
                                "calendarDate": "2026-10-01",
                                "weight": 93599.0,
                                "timestampGMT": 1790834400000,
                            },
                            {"samplePk": 12, "calendarDate": "2026-10-01", "weight": 93100.0},
                        ],
                    },
                    {"summaryDate": "2026-10-02", "allWeightMetrics": None},
                ],
                "totalAverage": {},
            },
            [("11", "2026-10-01", 1790834400), ("12", "2026-10-01", None)],
        ),
        (
            "activities",
            "/activitylist-service/activities/search/activities",
            [
                {
                    "activityId": 987,
                    "startTimeLocal": "2026-10-02 07:15:00",
                    "beginTimestamp": 1790917200000,
                }
            ],
            [("987", "2026-10-02", 1790917200)],
        ),
    ],
)
async def test_feed_parses_response(
    ctx: sync.SyncContext,
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    feed: str,
    path: str,
    body: object,
    expected: list[tuple[str, str, int | None]],
) -> None:
    garmin_api.serve(path, body)

    assert await sync.sync_feed(ctx, sync.FEEDS[feed], OCT1, OCT2) == len(expected)

    assert stored(conn, feed) == expected
    [request] = garmin_api.data_requests()
    if "startDate" in request.url.params:
        assert request.url.params["startDate"] == "2026-10-01"
        assert request.url.params["endDate"] == "2026-10-02"


@pytest.mark.anyio
async def test_raw_json_is_kept_whole(
    ctx: sync.SyncContext, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    record = {"calendarDate": "2026-10-01", "sleepScores": {"overall": {"value": 82}}, "x": [1, 2]}
    garmin_api.serve("/sleep-service/sleep/dailySleepSummariesByDate", [record])

    await sync.sync_feed(ctx, sync.FEEDS["sleeps"], OCT1, OCT1)

    raw = conn.execute("SELECT raw_json FROM summaries").fetchone()[0]
    assert json.loads(raw) == record


@pytest.mark.anyio
async def test_records_without_key_are_skipped(
    ctx: sync.SyncContext, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve(
        "/sleep-service/sleep/dailySleepSummariesByDate",
        [{"calendarDate": "2026-10-01"}, {"sleepTimeSeconds": 1}, "junk"],
    )
    assert await sync.sync_feed(ctx, sync.FEEDS["sleeps"], OCT1, OCT1) == 1


@pytest.mark.anyio
async def test_unexpected_shape_is_an_error(
    ctx: sync.SyncContext, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve("/usersummary-service/v2/dailysummaries", {"data": "nope"})

    results = await sync.run_sync(ctx, {"dailies": (OCT1, OCT1)})

    assert results["dailies"].startswith("error")


# --- chunking and paging ---


def test_chunks_respect_max_days() -> None:
    assert sync.chunks(date(2026, 1, 1), date(2026, 2, 10), 31) == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 10)),
    ]
    assert sync.chunks(OCT1, OCT1, 28) == [(OCT1, OCT1)]
    assert sync.chunks(OCT2, OCT1, 28) == []


@pytest.mark.anyio
async def test_long_range_is_split_into_documented_maxima(
    ctx: sync.SyncContext, garmin_api: FakeGarminAPI
) -> None:
    path = "/sleep-service/sleep/dailySleepSummariesByDate"
    garmin_api.serve(path, [])

    await sync.sync_feed(ctx, sync.FEEDS["sleeps"], date(2026, 1, 1), date(2026, 3, 15))

    ranges = [
        (r.url.params["startDate"], r.url.params["endDate"]) for r in garmin_api.data_requests()
    ]
    assert ranges == [
        ("2026-01-01", "2026-01-31"),
        ("2026-02-01", "2026-03-03"),
        ("2026-03-04", "2026-03-15"),
    ]


@pytest.mark.anyio
async def test_activities_are_paged(
    ctx: sync.SyncContext, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["start"])
        count = min(sync.ACTIVITY_PAGE_SIZE, 130 - offset)
        return httpx.Response(
            200,
            json=[
                {"activityId": offset + i, "startTimeLocal": "2026-10-01 08:00:00"}
                for i in range(count)
            ],
        )

    garmin_api.route("/activitylist-service/activities/search/activities", page)

    assert await sync.sync_feed(ctx, sync.FEEDS["activities"], OCT1, OCT1) == 130
    offsets = [r.url.params["start"] for r in garmin_api.data_requests()]
    assert offsets == ["0", "100"]


# --- scheduling ---


def test_first_scheduled_sync_catches_up() -> None:
    today = date(2026, 10, 25)
    ranges = sync.scheduled_ranges({}, today, lookback_days=3)

    assert set(ranges) == set(sync.FEEDS)
    assert ranges["sleeps"] == (date(2026, 7, 27), date(2026, 10, 26))


def test_scheduled_sync_rewinds_lookback_and_heals_downtime() -> None:
    today = date(2026, 10, 25)
    state = {
        "dailies": {"synced_through": "2026-10-26"},  # up to date
        "sleeps": {"synced_through": "2026-10-10"},  # server was down for two weeks
        "hrv": {"synced_through": "2025-01-01"},  # down for ages: capped catch-up
    }
    ranges = sync.scheduled_ranges(state, today, lookback_days=3)

    assert ranges["dailies"] == (date(2026, 10, 22), date(2026, 10, 26))
    assert ranges["sleeps"] == (date(2026, 10, 7), date(2026, 10, 26))
    assert ranges["hrv"][0] == date(2026, 7, 27)


@pytest.mark.anyio
async def test_failing_feed_does_not_block_others(
    ctx: sync.SyncContext, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve("/usersummary-service/v2/dailysummaries", status_code=400)
    garmin_api.serve(
        "/sleep-service/sleep/dailySleepSummariesByDate", [{"calendarDate": "2026-10-01"}]
    )

    results = await sync.run_sync(ctx, {"dailies": (OCT1, OCT2), "sleeps": (OCT1, OCT2)})

    assert results == {
        "dailies": "error: GET /usersummary-service/v2/dailysummaries: HTTP 400",
        "sleeps": 1,
    }
    state = storage.sync_state(conn)
    assert state["sleeps"]["synced_through"] == "2026-10-02"
    assert state["dailies"]["synced_through"] is None
    assert "HTTP 400" in state["dailies"]["last_error"]


def test_key_expiry_warning(caplog: pytest.LogCaptureFixture) -> None:
    today = date(2026, 10, 25)
    sync.warn_if_key_expiring(date(2027, 1, 1), today)
    assert caplog.text == ""

    sync.warn_if_key_expiring(date(2026, 11, 1), today)
    assert "expires in 7 days" in caplog.text

    sync.warn_if_key_expiring(date(2026, 10, 1), today)
    assert "expired on 2026-10-01" in caplog.text


# --- /admin/sync ---


def test_manual_sync_requires_admin(client: TestClient) -> None:
    body = {"start": "2026-10-01", "end": "2026-10-02"}
    assert client.post("/admin/sync", json=body).status_code == 401


def test_manual_sync_pulls_requested_feeds(
    client: TestClient,
    conn: sqlite3.Connection,
    garmin_api: FakeGarminAPI,
    admin_auth: tuple[str, str],
) -> None:
    garmin_api.serve(
        "/hrv-service/hrv/daily/2026-10-01/2026-10-02",
        {"hrvSummaries": [{"calendarDate": "2026-10-01"}]},
    )

    response = client.post(
        "/admin/sync",
        json={"start": "2026-10-01", "end": "2026-10-02", "feeds": ["hrv"]},
        auth=admin_auth,
    )

    assert response.status_code == 202
    assert response.json() == {"feeds": ["hrv"], "start": "2026-10-01", "end": "2026-10-02"}
    assert stored(conn, "hrv") == [("2026-10-01", "2026-10-01", None)]


@pytest.mark.parametrize(
    "body",
    [
        {"start": "2026-10-01", "end": "2026-10-02", "feeds": ["menstrualcycle"]},
        {"start": "2026-10-02", "end": "2026-10-01"},
        {"start": "2024-01-01", "end": "2026-10-01"},
    ],
)
def test_manual_sync_validates(
    client: TestClient,
    garmin_api: FakeGarminAPI,
    admin_auth: tuple[str, str],
    body: dict[str, object],
) -> None:
    assert client.post("/admin/sync", json=body, auth=admin_auth).status_code == 422
    assert garmin_api.requests == []


@pytest.mark.anyio
async def test_scheduled_sync_covers_every_feed(
    ctx: sync.SyncContext, conn: sqlite3.Connection, garmin_api: FakeGarminAPI
) -> None:
    # Every endpoint answers "no data" in its documented shape.
    def empty(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/hrv-service/"):
            return httpx.Response(200, json={"hrvSummaries": []})
        if path.endswith("/dailysummaries"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=[])

    garmin_api.default = empty

    results = await sync.scheduled_sync(ctx, lookback_days=3)

    assert results == {name: 0 for name in sync.FEEDS}
    state = storage.sync_state(conn)
    assert set(state) == set(sync.FEEDS)
    # FakeClock's now is 2027-01-15, so every feed is synced through tomorrow.
    assert {s["synced_through"] for s in state.values()} == {"2027-01-16"}
