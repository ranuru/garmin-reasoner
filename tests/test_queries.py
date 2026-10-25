import sqlite3
from datetime import date

from garmin_reasoner import queries, storage


def add(
    conn: sqlite3.Connection, summary_type: str, summary_id: str, day: str, **fields: object
) -> None:
    storage.upsert_summary(conn, summary_type, summary_id, fields, "t", day)


def test_range_is_inclusive_and_ordered(conn: sqlite3.Connection) -> None:
    for day in ["2026-10-03", "2026-10-01", "2026-10-02", "2026-10-04"]:
        add(conn, "dailies", day, day, calendarDate=day)
    add(conn, "sleeps", "s", "2026-10-02")

    result = queries.summaries_in_range(conn, "dailies", date(2026, 10, 1), date(2026, 10, 3))

    assert [s["calendarDate"] for s in result] == ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_hides_identifiers_and_gps(conn: sqlite3.Connection) -> None:
    add(
        conn,
        "activities",
        "1",
        "2026-10-01",
        activityId=1,
        activityName="Morning Run",
        ownerFullName="Someone",
        userProfilePk=123,
        startLatitude=59.9,
        startLongitude=10.7,
        locationName="Oslo",
    )

    [activity] = queries.summaries_in_range(
        conn, "activities", date(2026, 10, 1), date(2026, 10, 1)
    )

    assert activity == {"activityId": 1, "activityName": "Morning Run", "locationName": "Oslo"}


def test_date_span(conn: sqlite3.Connection) -> None:
    add(conn, "dailies", "a", "2026-10-03")
    add(conn, "sleeps", "b", "2026-09-30")
    add(conn, "stress", "c", "2026-08-01")

    assert queries.date_span(conn, ["dailies", "sleeps"]) == (date(2026, 9, 30), date(2026, 10, 3))
    assert queries.date_span(conn, ["hrv"]) is None


def test_overview(conn: sqlite3.Connection) -> None:
    add(conn, "dailies", "a", "2026-10-01")
    add(conn, "dailies", "b", "2026-10-03")
    add(conn, "hrv", "h", "2026-10-02")

    assert queries.data_overview(conn) == [
        {"type": "dailies", "count": 2, "first_date": "2026-10-01", "last_date": "2026-10-03"},
        {"type": "hrv", "count": 1, "first_date": "2026-10-02", "last_date": "2026-10-02"},
    ]
