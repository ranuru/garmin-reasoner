"""Compact, LLM-friendly views over the raw summaries.

One flat row per night, day or activity, plus per-week/month aggregates. Everything is
derived from raw JSON at query time. Every row has the same keys, with None for values
Garmin didn't record, and units are in the key names.
"""

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from garmin_reasoner import queries

Row = dict[str, Any]
Record = dict[str, Any]
GroupBy = Literal["week", "month", "none"]

# Bedtimes are compared on a clock that starts at noon, so 23:30 and 00:30 are an hour
# apart rather than 23 hours.
BEDTIME_PIVOT_MINUTES = 12 * 60


def _get(record: Record | None, *path: str) -> Any:
    value: Any = record
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _num(value: Any, digits: int = 0) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(value) if digits == 0 else round(value, digits)


def _minutes(seconds: Any) -> int | None:
    return _num(seconds / 60) if _num(seconds) is not None else None


def _level(value: Any) -> int | None:
    """Garmin uses negative stress/Body Battery levels for "not enough data"."""
    level = _num(value)
    return level if level is not None and level >= 0 else None


def _local_clock(local_ms: Any) -> str | None:
    """HH:MM from one of Garmin's `...TimestampLocal` values.

    Those are epoch milliseconds with the local wall-clock time encoded as if it were
    UTC, so reading them as UTC gives the local time, with whatever offset applied at
    that moment. A night that crosses a DST change gets the right time at both ends.
    """
    if _num(local_ms) is None:
        return None
    return datetime.fromtimestamp(local_ms / 1000, UTC).strftime("%H:%M")


def _days(start: date, end: date) -> Iterator[str]:
    day = start
    while day <= end:
        yield day.isoformat()
        day += timedelta(days=1)


def _by_date(
    conn: sqlite3.Connection, summary_type: str, start: date, end: date
) -> dict[str, Record]:
    """One record per calendar date (the last one stored, if there are several)."""
    return dict(queries.dated_summaries_in_range(conn, summary_type, start, end))


def _all_by_date(
    conn: sqlite3.Connection, summary_type: str, start: date, end: date
) -> dict[str, list[Record]]:
    grouped: dict[str, list[Record]] = defaultdict(list)
    for day, record in queries.dated_summaries_in_range(conn, summary_type, start, end):
        grouped[day].append(record)
    return grouped


def _result(start: date, end: date, rows: list[Row], covered: set[str] | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(rows),
    }
    if covered is not None:
        result["missing"] = [day for day in _days(start, end) if day not in covered]
    result["rows"] = rows
    return result


# --- sleep ---


def sleep_row(day: str, sleep: Record | None, hrv: Record | None) -> Row:
    """One night, dated by the morning it ends. Either record may be missing."""
    return {
        "date": day,
        "bedtime": _local_clock(_get(sleep, "sleepStartTimestampLocal")),
        "wake": _local_clock(_get(sleep, "sleepEndTimestampLocal")),
        "sleep_min": _minutes(_get(sleep, "sleepTimeSeconds")),
        "deep_min": _minutes(_get(sleep, "deepSleepSeconds")),
        "light_min": _minutes(_get(sleep, "lightSleepSeconds")),
        "rem_min": _minutes(_get(sleep, "remSleepSeconds")),
        "awake_min": _minutes(_get(sleep, "awakeSleepSeconds")),
        "score": _num(_get(sleep, "sleepScores", "overall", "value")),
        "avg_stress": _level(_get(sleep, "avgSleepStress")),
        "avg_hr": _num(_get(sleep, "avgHeartRate")),
        "avg_resp": _num(_get(sleep, "averageRespirationValue"), 1),
        "hrv": _num(_get(hrv, "lastNightAvg")),
        "nap_min": _minutes(_get(sleep, "napTimeSeconds")),
    }


def sleep_summary(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    sleeps = _by_date(conn, "sleeps", start, end)
    hrv = _by_date(conn, "hrv", start, end)
    covered = sleeps.keys() | hrv.keys()
    rows = [
        sleep_row(day, sleeps.get(day), hrv.get(day)) for day in _days(start, end) if day in covered
    ]
    return _result(start, end, rows, covered)


# --- activities ---


def _activity_type(activity: Record) -> str | None:
    return _get(activity, "activityType", "typeKey")


def _is_run(activity: Record) -> bool:
    # running, trail_running, treadmill_running, track_running, virtual_run, ...
    return "run" in (_activity_type(activity) or "")


def _pace(meters_per_second: float) -> str:
    """min:sec per km."""
    seconds = round(1000 / meters_per_second)
    return f"{seconds // 60}:{seconds % 60:02d}"


def activity_row(activity: Record) -> Row:
    distance = _num(_get(activity, "distance"), 2)
    has_distance = distance is not None and distance > 0
    speed = _num(_get(activity, "averageSpeed"), 3)
    has_speed = has_distance and speed is not None and speed > 0
    duration = _num(_get(activity, "duration"))
    label = _get(activity, "trainingEffectLabel")
    start = _get(activity, "startTimeLocal")
    return {
        "start": str(start)[:16] if start else None,
        "type": _activity_type(activity),
        "name": _get(activity, "activityName"),
        "duration_min": round(duration / 60, 1) if duration is not None else None,
        "distance_km": round(distance / 1000, 2) if has_distance else None,
        "pace_min_km": _pace(speed) if has_speed and _is_run(activity) else None,
        "speed_kmh": round(speed * 3.6, 1) if has_speed and not _is_run(activity) else None,
        "avg_hr": _num(_get(activity, "averageHR")),
        "max_hr": _num(_get(activity, "maxHR")),
        "elev_gain_m": _num(_get(activity, "elevationGain")),
        "training_effect": label.lower() if isinstance(label, str) and label != "UNKNOWN" else None,
        **{
            f"z{zone}_min": _minutes(_get(activity, f"hrTimeInZone_{zone}")) for zone in range(1, 6)
        },
    }


def activity_list(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    rows = [activity_row(a) for a in queries.summaries_in_range(conn, "activities", start, end)]
    return _result(start, end, rows, covered=None)


# --- days ---


def _activity_brief(activity: Record) -> str:
    minutes = _minutes(_get(activity, "duration"))
    kind = _activity_type(activity) or "activity"
    return f"{kind} {minutes}min" if minutes is not None else kind


def daily_row(day: str, daily: Record | None, activities: list[Record]) -> Row:
    return {
        "date": day,
        "steps": _num(_get(daily, "movement", "steps", "value")),
        "floors": _num(_get(daily, "movement", "floorsAscended", "value")),
        "resting_hr": _num(_get(daily, "heartRate", "restingValue")),
        "im_moderate": _num(_get(daily, "intensityMinutes", "moderate")),
        "im_vigorous": _num(_get(daily, "intensityMinutes", "vigorous")),
        "active_kcal": _num(_get(daily, "calories", "burnedActive")),
        "bb_max": _level(_get(daily, "bodyBattery", "maxValue")),
        "bb_min": _level(_get(daily, "bodyBattery", "minValue")),
        "stress": _level(_get(daily, "stress", "avgLevel")),
        "activities": [_activity_brief(a) for a in activities],
    }


def daily_overview(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    dailies = _by_date(conn, "dailies", start, end)
    activities = _all_by_date(conn, "activities", start, end)
    covered = dailies.keys() | activities.keys()
    rows = [
        daily_row(day, dailies.get(day), activities.get(day, []))
        for day in _days(start, end)
        if day in covered
    ]
    return _result(start, end, rows, covered)


# --- periods ---


def periods(start: date, end: date, group_by: GroupBy) -> list[tuple[str, date, date]]:
    """(label, first day, last day) per ISO week or calendar month, clipped to the range."""
    if group_by == "none":
        return [("all", start, end)]
    result = []
    first = start
    while first <= end:
        if group_by == "week":
            last = first + timedelta(days=6 - first.weekday())
            year, week, _ = first.isocalendar()
            label = f"{year}-W{week:02d}"
        elif group_by == "month":
            next_month = date(first.year + first.month // 12, first.month % 12 + 1, 1)
            last = next_month - timedelta(days=1)
            label = f"{first.year}-{first.month:02d}"
        else:
            raise ValueError(f"group_by must be week, month or none, not {group_by!r}")
        last = min(last, end)
        result.append((label, first, last))
        first = last + timedelta(days=1)
    return result


def _bedtime_minutes(clock: str) -> int:
    hours, minutes = map(int, clock.split(":"))
    total = hours * 60 + minutes
    return total + 24 * 60 if total < BEDTIME_PIVOT_MINUTES else total


def _clock(minutes: float) -> str:
    total = round(minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _avg_median(name: str, values: list[Any], digits: int = 0) -> Row:
    present = [v for v in values if v is not None]
    if not present:
        return {f"{name}_avg": None, f"{name}_median": None}
    return {
        f"{name}_avg": _num(statistics.fmean(present), digits),
        f"{name}_median": _num(statistics.median(present), digits),
    }


def _bedtime_stats(bedtimes: list[str | None]) -> Row:
    minutes = [_bedtime_minutes(b) for b in bedtimes if b is not None]
    if not minutes:
        return {"bedtime_avg": None, "bedtime_median": None}
    return {
        "bedtime_avg": _clock(statistics.fmean(minutes)),
        "bedtime_median": _clock(statistics.median(minutes)),
    }


def _sum(values: list[Any]) -> int | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _weight_kg(weigh_in: Record) -> float | None:
    grams = _num(_get(weigh_in, "weight"))  # grams, despite the spec saying kg
    return round(grams / 1000, 1) if grams is not None else None


def period_row(
    label: str,
    first: date,
    last: date,
    nights: list[Row],
    days: list[Row],
    activities: list[Record],
    weigh_ins: list[Record],
) -> Row:
    sleep_minutes = [n["sleep_min"] for n in nights]
    slept = [m for m in sleep_minutes if m is not None]
    moderate = _sum([d["im_moderate"] for d in days])
    vigorous = _sum([d["im_vigorous"] for d in days])
    # As in Garmin's weekly intensity goal, vigorous minutes count twice.
    im_total = None
    if moderate is not None or vigorous is not None:
        im_total = (moderate or 0) + 2 * (vigorous or 0)
    activity_types = Counter(_activity_type(a) or "other" for a in activities)
    weights = [w for w in map(_weight_kg, weigh_ins) if w is not None]
    return {
        "period": label,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "days": (last - first).days + 1,
        "nights": len(slept),
        **_avg_median("sleep_min", sleep_minutes),
        **_avg_median("sleep_score", [n["score"] for n in nights]),
        **_bedtime_stats([n["bedtime"] for n in nights]),
        **_avg_median("resting_hr", [d["resting_hr"] for d in days]),
        **_avg_median("hrv", [n["hrv"] for n in nights]),
        **_avg_median("steps", [d["steps"] for d in days]),
        **_avg_median("stress", [d["stress"] for d in days]),
        "nights_under_7h": sum(m < 7 * 60 for m in slept),
        "nights_under_6h": sum(m < 6 * 60 for m in slept),
        "activities": dict(activity_types.most_common()),
        "im_moderate": moderate,
        "im_vigorous": vigorous,
        "im_total": im_total,
        "weight_first_kg": weights[0] if weights else None,
        "weight_last_kg": weights[-1] if weights else None,
    }


PERIOD_STATS_TYPES = ["sleeps", "hrv", "dailies", "activities", "weight"]


def period_stats(
    conn: sqlite3.Connection, start: date, end: date, group_by: GroupBy
) -> dict[str, Any]:
    """Stats per period, for the part of [start, end] that has data.

    The range is clipped to the first and last stored date, so asking for "all time"
    returns rows for the history that exists rather than empty periods before it.
    """
    span = queries.date_span(conn, PERIOD_STATS_TYPES)
    if span is None or span[0] > end or span[1] < start:
        return {"group_by": group_by, **_result(start, end, [], covered=None)}
    start, end = max(start, span[0]), min(end, span[1])

    nights = {row["date"]: row for row in sleep_summary(conn, start, end)["rows"]}
    days = {row["date"]: row for row in daily_overview(conn, start, end)["rows"]}
    activities = _all_by_date(conn, "activities", start, end)
    weigh_ins = _all_by_date(conn, "weight", start, end)
    rows = []
    for label, first, last in periods(start, end, group_by):
        in_group = list(_days(first, last))
        group_weigh_ins = [w for d in in_group for w in weigh_ins.get(d, [])]
        rows.append(
            period_row(
                label,
                first,
                last,
                [nights[d] for d in in_group if d in nights],
                [days[d] for d in in_group if d in days],
                [a for d in in_group for a in activities.get(d, [])],
                sorted(group_weigh_ins, key=lambda w: _num(_get(w, "timestampGMT")) or 0),
            )
        )
    return {"group_by": group_by, **_result(start, end, rows, covered=None)}


# --- output ---


def to_text(result: dict[str, Any]) -> str:
    """Compact JSON with one row per line: no indentation, no repeated whitespace."""

    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    head = dump({k: v for k, v in result.items() if k != "rows"})
    rows = ",\n".join(dump(row) for row in result["rows"])
    return f'{head[:-1]},"rows":[\n{rows}\n]}}' if rows else f'{head[:-1]},"rows":[]}}'
