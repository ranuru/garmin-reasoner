import json
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from garmin_reasoner import compact, storage

# --- fixture records, shaped like the Personal API's (see docs/garmin-personal-api.json) ---


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def sleep_record(
    day: str,
    bed: datetime,
    bed_offset_hours: int,
    wake: datetime,
    wake_offset_hours: int,
    **overrides: Any,
) -> dict[str, Any]:
    """`bed` and `wake` are naive local wall-clock times with their UTC offsets."""

    def gmt(local: datetime, offset: int) -> int:
        return _epoch_ms(local.replace(tzinfo=timezone(timedelta(hours=offset))))

    def local_as_utc(local: datetime) -> int:
        return _epoch_ms(local.replace(tzinfo=UTC))

    record = {
        "calendarDate": day,
        "userProfilePK": 123,
        "sleepTimeSeconds": 25_500,
        "napTimeSeconds": 0,
        "sleepStartTimestampGMT": gmt(bed, bed_offset_hours),
        "sleepEndTimestampGMT": gmt(wake, wake_offset_hours),
        "sleepStartTimestampLocal": local_as_utc(bed),
        "sleepEndTimestampLocal": local_as_utc(wake),
        "deepSleepSeconds": 4_320,
        "lightSleepSeconds": 15_000,
        "remSleepSeconds": 6_180,
        "awakeSleepSeconds": 720,
        "averageSpO2Value": 95.0,
        "averageRespirationValue": 14.06,
        "avgSleepStress": 14.4,
        "avgHeartRate": 52.4,
        "sleepScores": {
            "overall": {"value": 82, "qualifierKey": "GOOD"},
            "deepPercentage": {"value": 17, "qualifierKey": "GOOD"},
        },
        "sleepNeed": {"baseline": 480, "actual": 470, "feedback": "NO_CHANGE"},
    }
    record.update(overrides)
    return record


def night(day: str, bedtime: str, wake: str = "08:00", **overrides: Any) -> dict[str, Any]:
    """A night ending on `day` with no DST change; bedtimes from 12:00 are the evening before."""
    wake_day = date.fromisoformat(day)
    bed_h, bed_m = map(int, bedtime.split(":"))
    bed_day = wake_day - timedelta(days=1) if bed_h >= 12 else wake_day
    wake_h, wake_m = map(int, wake.split(":"))
    return sleep_record(
        day,
        datetime(bed_day.year, bed_day.month, bed_day.day, bed_h, bed_m),
        2,
        datetime(wake_day.year, wake_day.month, wake_day.day, wake_h, wake_m),
        2,
        **overrides,
    )


def hrv_record(day: str, last_night: int = 61) -> dict[str, Any]:
    return {
        "calendarDate": day,
        "weeklyAvg": 66,
        "lastNightAvg": last_night,
        "lastNight5MinHigh": 90,
        "baseline": {"lowUpper": 52, "balancedLow": 57, "balancedUpper": 80},
        "status": "BALANCED",
    }


def daily_record(day: str, **overrides: Any) -> dict[str, Any]:
    record = {
        "calendarDate": day,
        "userProfilePk": 123,
        "movement": {
            "steps": {"goal": 6610, "value": 8802, "distanceInMeters": 6742.0},
            "floorsAscended": {"goal": 10, "value": 7, "distanceInMeters": 23.2},
        },
        "calories": {"burnedResting": 2411, "burnedActive": 650, "burnedTotal": 3061},
        "heartRate": {"minValue": 41, "maxValue": 123, "restingValue": 42},
        "intensityMinutes": {"goal": 150, "moderate": 49, "vigorous": 5},
        "stress": {"avgLevel": 30, "maxLevel": 96, "qualifier": "BALANCED"},
        "bodyBattery": {"minValue": 20, "maxValue": 100, "chargedValue": 70},
        "respiration": {"avgValue": 15},
    }
    record.update(overrides)
    return record


RUN = {
    "activityId": 1001,
    "activityName": "Evening Run",
    "startTimeLocal": "2026-10-22 14:35:46",
    "startTimeGMT": "2026-10-22 12:35:46",
    "activityType": {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
    "distance": 4066.95,
    "duration": 1621.893,
    "movingDuration": 1612.3,
    "elevationGain": 24.0,
    "averageSpeed": 2.508,
    "averageHR": 164.0,
    "maxHR": 177.0,
    "trainingEffectLabel": "LACTATE_THRESHOLD",
    "hrTimeInZone_1": 6.199,
    "hrTimeInZone_2": 43.0,
    "hrTimeInZone_3": 298.009,
    "hrTimeInZone_4": 1258.644,
    "hrTimeInZone_5": 16.0,
    "beginTimestamp": 1_792_672_546_000,
    "ownerId": 123,
    "startLatitude": 60.39,
    "startLongitude": 5.32,
}


def activity(**overrides: Any) -> dict[str, Any]:
    return {**RUN, **overrides}


HIKE = activity(
    activityId=1002,
    activityName="Hike",
    startTimeLocal="2026-10-22 08:00:00",
    activityType={"typeId": 3, "typeKey": "hiking", "parentTypeId": 17},
    distance=6195.63,
    duration=5678.5,
    averageSpeed=1.091,
    elevationGain=285.0,
    trainingEffectLabel="AEROBIC_BASE",
)
STRENGTH = activity(
    activityId=1003,
    activityName="Strength",
    startTimeLocal="2026-10-21 10:11:14",
    activityType={"typeId": 13, "typeKey": "strength_training", "parentTypeId": 29},
    distance=0.0,
    duration=2460.77,
    averageSpeed=0.0,
    elevationGain=None,
    trainingEffectLabel="UNKNOWN",
)


def store(conn: sqlite3.Connection, summary_type: str, record: dict[str, Any]) -> None:
    """Store a record keyed the way `sync.FEEDS` keys it."""
    start_time = None
    if summary_type == "activities":
        key, day = str(record["activityId"]), record["startTimeLocal"][:10]
        start_time = int(datetime.fromisoformat(record["startTimeLocal"]).timestamp())
    elif summary_type == "weight":
        key, day = str(record["samplePk"]), record["calendarDate"]
    else:
        key = day = record["calendarDate"]
    storage.upsert_summary(conn, summary_type, key, record, "t", day, start_time)


def weigh_in(sample: int, day: str, grams: float, hour: int = 7) -> dict[str, Any]:
    moment = datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)
    return {
        "samplePk": sample,
        "calendarDate": day,
        "weight": grams,
        "timestampGMT": _epoch_ms(moment),
        "sourceType": "MANUAL",
    }


D = date.fromisoformat


# --- sleep ---


def test_sleep_row_has_every_field() -> None:
    row = compact.sleep_row(
        "2026-10-23", night("2026-10-23", "00:57", "08:03"), hrv_record("x", 80)
    )

    assert row == {
        "date": "2026-10-23",
        "bedtime": "00:57",
        "wake": "08:03",
        "sleep_min": 425,
        "deep_min": 72,
        "light_min": 250,
        "rem_min": 103,
        "awake_min": 12,
        "score": 82,
        "avg_stress": 14,
        "avg_hr": 52,
        "avg_resp": 14.1,
        "hrv": 80,
        "nap_min": 0,
    }


def test_night_across_fall_back_uses_each_ends_own_offset() -> None:
    # Europe/Oslo leaves summer time at 03:00 CEST (+2) -> 02:00 CET (+1) on 2026-10-25.
    sleep = sleep_record(
        "2026-10-25", datetime(2026, 10, 24, 23, 30), 2, datetime(2026, 10, 25, 7, 0), 1
    )
    # 7.5 hours on the wall clock, but 8.5 hours actually passed.
    assert sleep["sleepEndTimestampGMT"] - sleep["sleepStartTimestampGMT"] == 8.5 * 3_600_000

    row = compact.sleep_row("2026-10-25", sleep, None)

    assert (row["bedtime"], row["wake"]) == ("23:30", "07:00")


def test_bedtime_after_midnight_before_the_switch() -> None:
    sleep = sleep_record(
        "2026-10-25", datetime(2026, 10, 25, 1, 30), 2, datetime(2026, 10, 25, 9, 59), 1
    )

    row = compact.sleep_row("2026-10-25", sleep, None)

    assert (row["bedtime"], row["wake"]) == ("01:30", "09:59")


def test_night_across_spring_forward() -> None:
    # 02:00 CET (+1) -> 03:00 CEST (+2) on 2026-03-29.
    sleep = sleep_record(
        "2026-03-29", datetime(2026, 3, 29, 1, 45), 1, datetime(2026, 3, 29, 8, 15), 2
    )

    row = compact.sleep_row("2026-03-29", sleep, None)

    assert (row["bedtime"], row["wake"]) == ("01:45", "08:15")


def test_sleep_record_without_details_gives_nulls_not_missing_keys() -> None:
    sparse = {"calendarDate": "2026-10-20", "sleepTimeSeconds": 18_000}

    row = compact.sleep_row("2026-10-20", sparse, None)

    assert row["sleep_min"] == 300
    assert set(row) == set(compact.sleep_row("x", night("2026-10-20", "23:00"), None))
    assert all(row[k] is None for k in row if k not in ("date", "sleep_min"))


def test_sleep_summary_joins_hrv_and_reports_missing_nights(conn: sqlite3.Connection) -> None:
    store(conn, "sleeps", night("2026-10-20", "23:10"))
    store(conn, "hrv", hrv_record("2026-10-20", 70))
    store(conn, "sleeps", night("2026-10-21", "00:30"))  # no HRV that night
    store(conn, "hrv", hrv_record("2026-10-22", 64))  # HRV but no sleep record
    # 2026-10-23: nothing at all

    result = compact.sleep_summary(conn, D("2026-10-20"), D("2026-10-23"))

    assert [(r["date"], r["sleep_min"], r["hrv"]) for r in result["rows"]] == [
        ("2026-10-20", 425, 70),
        ("2026-10-21", 425, None),
        ("2026-10-22", None, 64),
    ]
    assert result["missing"] == ["2026-10-23"]
    assert result["count"] == 3


# --- days ---


def test_daily_row() -> None:
    row = compact.daily_row("2026-10-22", daily_record("2026-10-22"), [HIKE, RUN])

    assert row == {
        "date": "2026-10-22",
        "steps": 8802,
        "floors": 7,
        "resting_hr": 42,
        "im_moderate": 49,
        "im_vigorous": 5,
        "active_kcal": 650,
        "bb_max": 100,
        "bb_min": 20,
        "stress": 30,
        "activities": ["hiking 95min", "running 27min"],
    }


def test_daily_row_treats_negative_levels_as_missing() -> None:
    daily = daily_record("2026-10-22", stress={"avgLevel": -1}, bodyBattery={"minValue": -2})

    row = compact.daily_row("2026-10-22", daily, [])

    assert (row["stress"], row["bb_min"], row["bb_max"]) == (None, None, None)


def test_daily_overview_covers_days_with_any_data(conn: sqlite3.Connection) -> None:
    store(conn, "dailies", daily_record("2026-10-20"))
    store(conn, "activities", STRENGTH)  # 2026-10-21, no daily summary that day
    store(conn, "activities", HIKE)
    store(conn, "activities", RUN)
    store(conn, "dailies", daily_record("2026-10-22"))

    result = compact.daily_overview(conn, D("2026-10-19"), D("2026-10-22"))

    assert [(r["date"], r["steps"], r["activities"]) for r in result["rows"]] == [
        ("2026-10-20", 8802, []),
        ("2026-10-21", None, ["strength_training 41min"]),
        ("2026-10-22", 8802, ["hiking 95min", "running 27min"]),
    ]
    assert result["missing"] == ["2026-10-19"]


# --- activities ---


def test_run_has_pace_not_speed() -> None:
    row = compact.activity_row(RUN)

    assert row == {
        "start": "2026-10-22 14:35",
        "type": "running",
        "name": "Evening Run",
        "duration_min": 27.0,
        "distance_km": 4.07,
        "pace_min_km": "6:39",
        "speed_kmh": None,
        "avg_hr": 164,
        "max_hr": 177,
        "elev_gain_m": 24,
        "training_effect": "lactate_threshold",
        "z1_min": 0,
        "z2_min": 1,
        "z3_min": 5,
        "z4_min": 21,
        "z5_min": 0,
    }


def test_other_activities_have_speed_or_nothing() -> None:
    hike, strength = compact.activity_row(HIKE), compact.activity_row(STRENGTH)

    assert (hike["pace_min_km"], hike["speed_kmh"], hike["distance_km"]) == (None, 3.9, 6.2)
    assert (strength["distance_km"], strength["pace_min_km"], strength["speed_kmh"]) == (
        None,
        None,
        None,
    )
    assert strength["elev_gain_m"] is None
    assert strength["training_effect"] is None


def test_activity_list_never_includes_gps_or_ids(conn: sqlite3.Connection) -> None:
    store(conn, "activities", RUN)

    text = compact.to_text(compact.activity_list(conn, D("2026-10-22"), D("2026-10-22")))

    for hidden in ("60.39", "5.32", "startLatitude", "ownerId", "1001"):
        assert hidden not in text


# --- periods ---


def test_weeks_are_iso_and_clipped_to_the_range() -> None:
    assert compact.periods(D("2026-12-23"), D("2027-01-05"), "week") == [
        ("2026-W52", D("2026-12-23"), D("2026-12-27")),
        ("2026-W53", D("2026-12-28"), D("2027-01-03")),
        ("2027-W01", D("2027-01-04"), D("2027-01-05")),
    ]


def test_months_roll_over_the_year() -> None:
    assert compact.periods(D("2026-11-15"), D("2027-02-03"), "month") == [
        ("2026-11", D("2026-11-15"), D("2026-11-30")),
        ("2026-12", D("2026-12-01"), D("2026-12-31")),
        ("2027-01", D("2027-01-01"), D("2027-01-31")),
        ("2027-02", D("2027-02-01"), D("2027-02-03")),
    ]


def test_no_grouping_is_one_period() -> None:
    assert compact.periods(D("2026-10-01"), D("2026-10-25"), "none") == [
        ("all", D("2026-10-01"), D("2026-10-25"))
    ]


def test_period_stats(conn: sqlite3.Connection) -> None:
    # Week 2026-W43 (Mon 10-19 .. Sun 10-25), queried from Wednesday.
    store(conn, "sleeps", night("2026-10-21", "23:40", sleepTimeSeconds=7.5 * 3600))
    store(conn, "sleeps", night("2026-10-22", "00:20", sleepTimeSeconds=6.5 * 3600))
    store(conn, "sleeps", night("2026-10-23", "01:00", sleepTimeSeconds=5.5 * 3600))
    store(conn, "hrv", hrv_record("2026-10-21", 60))
    store(conn, "hrv", hrv_record("2026-10-23", 70))
    store(conn, "dailies", daily_record("2026-10-21"))
    store(
        conn,
        "dailies",
        daily_record(
            "2026-10-22",
            movement={"steps": {"value": 4000}},
            intensityMinutes={"moderate": 11, "vigorous": 20},
        ),
    )
    store(conn, "activities", STRENGTH)
    store(conn, "activities", RUN)
    store(conn, "activities", HIKE)
    store(conn, "activities", activity(activityId=1004, startTimeLocal="2026-10-23 18:00:00"))
    store(conn, "weight", weigh_in(2, "2026-10-22", 81_340.0, hour=20))
    store(conn, "weight", weigh_in(1, "2026-10-22", 80_960.0, hour=6))

    [week] = compact.period_stats(conn, D("2026-10-21"), D("2026-10-25"), "week")["rows"]

    assert week == {
        "period": "2026-W43",
        "start": "2026-10-21",
        "end": "2026-10-25",
        "days": 5,
        "nights": 3,
        "sleep_min_avg": 390,
        "sleep_min_median": 390,
        "sleep_score_avg": 82,
        "sleep_score_median": 82,
        "bedtime_avg": "00:20",
        "bedtime_median": "00:20",
        "resting_hr_avg": 42,
        "resting_hr_median": 42,
        "hrv_avg": 65,
        "hrv_median": 65,
        "steps_avg": 6401,
        "steps_median": 6401,
        "stress_avg": 30,
        "stress_median": 30,
        "nights_under_7h": 2,
        "nights_under_6h": 1,
        "activities": {"running": 2, "strength_training": 1, "hiking": 1},
        "im_moderate": 60,
        "im_vigorous": 25,
        "im_total": 110,
        "weight_first_kg": 81.0,
        "weight_last_kg": 81.3,
    }


def test_period_without_data_has_nulls_and_zero_counts(conn: sqlite3.Connection) -> None:
    result = compact.period_stats(conn, D("2026-10-01"), D("2026-10-31"), "month")

    [month] = result["rows"]
    assert result["group_by"] == "month"
    assert (month["nights"], month["nights_under_7h"], month["activities"]) == (0, 0, {})
    for key in ("sleep_min_avg", "bedtime_median", "hrv_avg", "im_total", "weight_last_kg"):
        assert month[key] is None


# --- output ---


def test_text_is_compact_json_with_one_row_per_line() -> None:
    result = {"start": "a", "end": "b", "count": 2, "rows": [{"x": 1, "y": None}, {"x": "Løp"}]}

    text = compact.to_text(result)

    assert json.loads(text) == result
    assert text.splitlines() == [
        '{"start":"a","end":"b","count":2,"rows":[',
        '{"x":1,"y":null},',
        '{"x":"Løp"}',
        "]}",
    ]
    assert json.loads(compact.to_text({**result, "count": 0, "rows": []}))["rows"] == []


def _fill_90_days(conn: sqlite3.Connection, end: date) -> date:
    start = end - timedelta(days=89)
    for i in range(90):
        day = (start + timedelta(days=i)).isoformat()
        store(conn, "sleeps", night(day, "23:41", "07:12", napTimeSeconds=1800))
        store(conn, "hrv", hrv_record(day))
        store(conn, "dailies", daily_record(day))
        for n, kind in enumerate((RUN, HIKE)):  # far more activities than a real 90 days
            store(
                conn,
                "activities",
                activity(
                    **{**kind, "activityId": i * 10 + n, "startTimeLocal": f"{day} 1{n}:00:00"}
                ),
            )
        store(conn, "weight", weigh_in(i, day, 80_000.0 + i * 10))
    return start


@pytest.mark.parametrize(
    "view",
    [
        compact.sleep_summary,
        compact.daily_overview,
        compact.activity_list,
        lambda conn, s, e: compact.period_stats(conn, s, e, "week"),
        lambda conn, s, e: compact.period_stats(conn, s, e, "none"),
    ],
)
def test_90_days_stays_well_under_25k_tokens(conn: sqlite3.Connection, view: Any) -> None:
    end = D("2026-10-25")
    start = _fill_90_days(conn, end)

    text = compact.to_text(view(conn, start, end))

    # JSON like this averages well over 3 characters per token.
    assert len(text) < 25_000 * 3
