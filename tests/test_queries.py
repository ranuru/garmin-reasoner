import sqlite3
from datetime import UTC, date, datetime

from garmin_reasoner import queries, storage


def add(conn: sqlite3.Connection, summary_type: str, summary_id: str, **fields: object) -> None:
    summary = {"summaryId": summary_id, "userId": "u1", "userAccessToken": "secret", **fields}
    storage.upsert_summary(conn, summary_type, summary, user_id="u1", received_at="t")


def test_range_is_inclusive_and_ordered(conn: sqlite3.Connection) -> None:
    for summary_id, day in [("c", "2026-10-03"), ("a", "2026-10-01"), ("b", "2026-10-02")]:
        add(conn, "dailies", summary_id, calendarDate=day)
    add(conn, "dailies", "z", calendarDate="2026-10-04")
    add(conn, "sleeps", "s", calendarDate="2026-10-02")

    result = queries.summaries_in_range(conn, "dailies", date(2026, 10, 1), date(2026, 10, 3))

    assert [s["summaryId"] for s in result] == ["a", "b", "c"]


def test_hides_user_identifiers(conn: sqlite3.Connection) -> None:
    add(conn, "dailies", "a", calendarDate="2026-10-01", steps=5)

    [summary] = queries.summaries_in_range(conn, "dailies", date(2026, 10, 1), date(2026, 10, 1))

    assert summary == {"summaryId": "a", "calendarDate": "2026-10-01", "steps": 5}


def test_activities_dated_by_local_start_time(conn: sqlite3.Connection) -> None:
    # 2026-10-01 23:30 UTC is already 2026-10-02 in Oslo (UTC+2).
    start = int(datetime(2026, 10, 1, 23, 30, tzinfo=UTC).timestamp())
    add(conn, "activities", "run", startTimeInSeconds=start, startTimeOffsetInSeconds=7200)

    oct1 = queries.summaries_in_range(conn, "activities", date(2026, 10, 1), date(2026, 10, 1))
    oct2 = queries.summaries_in_range(conn, "activities", date(2026, 10, 2), date(2026, 10, 2))

    assert oct1 == []
    assert [s["summaryId"] for s in oct2] == ["run"]


def test_present_dates_and_overview(conn: sqlite3.Connection) -> None:
    add(conn, "dailies", "a", calendarDate="2026-10-01")
    add(conn, "dailies", "b", calendarDate="2026-10-03")
    add(conn, "hrv", "h", calendarDate="2026-10-02")

    present = queries.present_dates(conn, "u1", "dailies", date(2026, 10, 1), date(2026, 10, 3))
    assert present == {date(2026, 10, 1), date(2026, 10, 3)}
    assert (
        queries.present_dates(conn, "other", "dailies", date(2026, 10, 1), date(2026, 10, 3))
        == set()
    )

    assert queries.data_overview(conn) == [
        {"type": "dailies", "count": 2, "first_date": "2026-10-01", "last_date": "2026-10-03"},
        {"type": "hrv", "count": 1, "first_date": "2026-10-02", "last_date": "2026-10-02"},
    ]
