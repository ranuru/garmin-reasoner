import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fakes import ACCESS_TOKEN, USER_ID, FakeGarminAPI
from fastapi.testclient import TestClient
from test_oauth import FakeClock

from garmin_reasoner import backfill, storage

BACKFILL_URL = "https://apis.garmin.com/wellness-api/rest/backfill"


class BackfillAPI(FakeGarminAPI):
    """Answers every backfill request with a fixed status."""

    def __init__(self, status_code: int = 202) -> None:
        super().__init__()
        self.status_code = status_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).startswith(BACKFILL_URL):
            return httpx.Response(self.status_code)
        return httpx.Response(404)

    def backfills(self) -> list[tuple[str, int, int]]:
        result = []
        for r in self.requests:
            params = parse_qs(urlsplit(str(r.url)).query)
            result.append(
                (
                    r.url.path.rsplit("/", 1)[-1],
                    int(params["summaryStartTimeInSeconds"][0]),
                    int(params["summaryEndTimeInSeconds"][0]),
                )
            )
        return result


@pytest.fixture
def garmin_api() -> BackfillAPI:
    return BackfillAPI()


def utc(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())


# --- /admin/backfill ---


def test_backfill_requires_admin(client: TestClient) -> None:
    body = {"summary_types": ["dailies"], "start": "2026-10-01", "end": "2026-10-01"}
    assert client.post("/admin/backfill", json=body).status_code == 401


@pytest.mark.usefixtures("known_user")
def test_backfill_requests_one_day_at_a_time(
    client: TestClient, garmin_api: BackfillAPI, admin_auth: tuple[str, str]
) -> None:
    body = {"summary_types": ["dailies", "sleeps"], "start": "2026-10-01", "end": "2026-10-02"}

    response = client.post("/admin/backfill", json=body, auth=admin_auth)

    assert response.status_code == 202
    assert response.json() == {"requests": 4}
    oct1, oct2, oct3 = utc("2026-10-01"), utc("2026-10-02"), utc("2026-10-03")
    assert garmin_api.backfills() == [
        ("dailies", oct1, oct2),
        ("dailies", oct2, oct3),
        ("sleeps", oct1, oct2),
        ("sleeps", oct2, oct3),
    ]
    assert all(end - start == 86400 for _, start, end in garmin_api.backfills())
    assert all(r.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}" for r in garmin_api.requests)


@pytest.mark.parametrize(
    "body",
    [
        {"summary_types": ["../user/registration"], "start": "2026-10-01", "end": "2026-10-01"},
        {"summary_types": [], "start": "2026-10-01", "end": "2026-10-01"},
        {"summary_types": ["dailies"], "start": "2026-10-02", "end": "2026-10-01"},
        {"summary_types": ["dailies"], "start": "2026-01-01", "end": "2026-12-31"},
    ],
)
def test_backfill_validates_request(
    client: TestClient,
    garmin_api: BackfillAPI,
    admin_auth: tuple[str, str],
    body: dict[str, object],
) -> None:
    assert client.post("/admin/backfill", json=body, auth=admin_auth).status_code == 422
    assert garmin_api.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status_code, outcome", [(202, "accepted"), (409, "duplicate"), (500, "failed")]
)
async def test_backfill_outcomes(
    settings, conn: sqlite3.Connection, known_user: str, status_code: int, outcome: str
) -> None:
    from garmin_reasoner.oauth import GarminClient

    api = BackfillAPI(status_code)
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))

    async def token(user_id: str) -> str:
        return ACCESS_TOKEN

    ctx = backfill.BackfillContext(
        settings.database_path, GarminClient(settings, http, FakeClock()), token, FakeClock(), 0
    )
    outcomes = await backfill.run_backfill(ctx, ["dailies"], date(2026, 10, 1), date(2026, 10, 1))
    assert outcomes == {outcome: 1}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- gap-check ---


def gap_client(make_client: Callable[..., TestClient], today: str) -> tuple[TestClient, FakeClock]:
    clock = FakeClock(utc(today) + 12 * 3600)
    return make_client(clock=clock), clock


def add_days(conn: sqlite3.Connection, summary_type: str, days: list[str]) -> None:
    for day in days:
        summary = {"summaryId": f"{summary_type}-{day}", "calendarDate": day}
        storage.upsert_summary(conn, summary_type, summary, USER_ID, "t")


@pytest.mark.usefixtures("known_user")
def test_gap_check_requests_missing_days_only(
    make_client: Callable[..., TestClient],
    conn: sqlite3.Connection,
    garmin_api: BackfillAPI,
    admin_auth: tuple[str, str],
    settings,
) -> None:
    settings.gap_check_days = 3  # checks 2026-10-18..20 when today is 2026-10-22
    client, _ = gap_client(make_client, "2026-10-22")
    for summary_type in backfill.GAP_CHECK_TYPES:
        add_days(conn, summary_type, ["2026-10-18", "2026-10-19", "2026-10-20"])
    conn.execute("DELETE FROM summaries WHERE type = 'sleeps' AND calendar_date = '2026-10-19'")
    conn.commit()

    response = client.post("/admin/gap-check", auth=admin_auth)

    assert response.json() == {"requested": [{"type": "sleeps", "date": "2026-10-19"}]}
    # Window centred on UTC midnight, so the local day is covered for any ±12h offset.
    midnight = utc("2026-10-19")
    assert garmin_api.backfills() == [("sleeps", midnight - 43200, midnight + 43200)]


@pytest.mark.usefixtures("known_user")
def test_gap_check_backs_off_and_gives_up(
    make_client: Callable[..., TestClient],
    conn: sqlite3.Connection,
    garmin_api: BackfillAPI,
    admin_auth: tuple[str, str],
    settings,
) -> None:
    settings.gap_check_days = 1  # only 2026-10-20, with nothing stored for it
    client, _ = gap_client(make_client, "2026-10-22")
    types = len(backfill.GAP_CHECK_TYPES)

    def requested() -> int:
        return len(client.post("/admin/gap-check", auth=admin_auth).json()["requested"])

    assert requested() == types
    assert requested() == 0  # asked recently; wait before retrying

    conn.execute("UPDATE backfill_requests SET last_requested_at = 0")
    conn.commit()
    assert requested() == types  # retry window passed

    conn.execute(
        "UPDATE backfill_requests SET last_requested_at = 0, attempts = ?",
        (backfill.MAX_GAP_ATTEMPTS,),
    )
    conn.commit()
    assert requested() == 0  # given up: probably no data for that day
