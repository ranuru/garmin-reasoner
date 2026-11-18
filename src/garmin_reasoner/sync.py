"""Pull data from the Garmin Personal API into SQLite.

Each feed is one endpoint with its own maximum date range (from the spec) and its own
record key. Ranges are split into chunks that fit, and every record is upserted, so
re-pulling a range is always safe.

The scheduled sync re-pulls from a few days before each feed's `synced_through` date up
to tomorrow. That picks up late uploads and also heals any downtime, however long,
without a separate gap-check.

Older history is pulled from the command line, on the server:

    docker compose exec garmin-reasoner python -m garmin_reasoner.sync 2025-01-01 2025-12-31
"""

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from garmin_reasoner import db, storage
from garmin_reasoner.garmin import Clock, GarminAPIError, GarminClient

if TYPE_CHECKING:
    from garmin_reasoner.config import Settings

log = logging.getLogger(__name__)

# (summary_id, calendar_date, start_time) for a record, or None if it has no usable key.
RecordKey = tuple[str, str | None, int | None] | None
Fetch = Callable[[GarminClient, date, date], Awaitable[list[dict[str, Any]]]]

# How far back the scheduled sync goes the first time, or after a very long outage.
MAX_CATCH_UP_DAYS = 90
ACTIVITY_PAGE_SIZE = 100


@dataclass(frozen=True)
class Feed:
    name: str
    max_days: int
    fetch: Fetch
    key: Callable[[dict[str, Any]], RecordKey]


def _records(body: Any, field: str | None = None) -> list[dict[str, Any]]:
    if field is not None:
        body = body.get(field) if isinstance(body, dict) else body
    if body is None:
        return []
    if not isinstance(body, list):
        raise GarminAPIError(f"expected a list{f' in {field!r}' if field else ''}")
    return [r for r in body if isinstance(r, dict)]


def _keyed_by_date(field: str) -> Callable[[dict[str, Any]], RecordKey]:
    def key(record: dict[str, Any]) -> RecordKey:
        day = record.get(field)
        return (str(day)[:10], str(day)[:10], None) if day else None

    return key


def _date_range(start: date, end: date) -> dict[str, str]:
    return {"startDate": start.isoformat(), "endDate": end.isoformat()}


async def _fetch_dailies(client: GarminClient, start: date, end: date) -> list[dict[str, Any]]:
    body = await client.get("/usersummary-service/v2/dailysummaries", _date_range(start, end))
    return _records(body, "data")


async def _fetch_sleeps(client: GarminClient, start: date, end: date) -> list[dict[str, Any]]:
    body = await client.get(
        "/sleep-service/sleep/dailySleepSummariesByDate", _date_range(start, end)
    )
    return _records(body)


async def _fetch_hrv(client: GarminClient, start: date, end: date) -> list[dict[str, Any]]:
    body = await client.get(f"/hrv-service/hrv/daily/{start}/{end}")
    return _records(body, "hrvSummaries")


async def _fetch_stress(client: GarminClient, start: date, end: date) -> list[dict[str, Any]]:
    body = await client.get(f"/usersummary-service/stats/stress/daily/{start}/{end}")
    return _records(body)


async def _fetch_body_battery(client: GarminClient, start: date, end: date) -> list[dict[str, Any]]:
    body = await client.get(
        "/wellness-service/wellness/bodyBattery/reports/daily", _date_range(start, end)
    )
    return [report for report in _records(body) if _has_body_battery_data(report)]


def _has_body_battery_data(report: dict[str, Any]) -> bool:
    """Garmin returns a report for every date asked for, even ones without data (such as
    before the first watch); in those, every value is null. Migration 7 in `db.py` uses
    the same test to remove ones stored earlier."""
    if any(report.get(field) is not None for field in ("charged", "drained", "startTimestampGMT")):
        return True
    levels = report.get("bodyBatteryValuesArray") or []
    return any(
        isinstance(point, list) and len(point) > 1 and point[1] is not None for point in levels
    )


async def _fetch_weight(client: GarminClient, start: date, end: date) -> list[dict[str, Any]]:
    # `weight/all` answers 400 with an empty body (seen 2026-10-25), so use `weight/range`
    # with every weigh-in included, flattened from its per-day grouping.
    body = await client.get(f"/weight-service/weight/range/{start}/{end}", {"includeAll": "true"})
    weigh_ins: list[dict[str, Any]] = []
    for day in _records(body, "dailyWeightSummaries"):
        weigh_ins.extend(_records(day.get("allWeightMetrics")))
    return weigh_ins


def _weight_key(record: dict[str, Any]) -> RecordKey:
    # Several weigh-ins per day are possible; samplePk identifies each one.
    sample, day = record.get("samplePk"), record.get("calendarDate")
    if sample is None or not day:
        return None
    timestamp = record.get("timestampGMT")
    start_time = int(timestamp) // 1000 if isinstance(timestamp, int | float) else None
    return str(sample), str(day)[:10], start_time


async def _fetch_activities(client: GarminClient, start: date, end: date) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    while True:
        page = _records(
            await client.get(
                "/activitylist-service/activities/search/activities",
                {
                    **_date_range(start, end),
                    "start": len(records),
                    "limit": ACTIVITY_PAGE_SIZE,
                    "sortOrder": "asc",
                },
            )
        )
        records.extend(page)
        if len(page) < ACTIVITY_PAGE_SIZE:
            return records


def _activity_key(record: dict[str, Any]) -> RecordKey:
    activity_id, local_start = record.get("activityId"), record.get("startTimeLocal")
    if activity_id is None or not local_start:
        return None
    begin_ms = record.get("beginTimestamp")
    return str(activity_id), str(local_start)[:10], int(begin_ms) // 1000 if begin_ms else None


# Ranges are the documented maxima; HRV, weight and activities document none, so they
# use a conservative chunk.
FEEDS: dict[str, Feed] = {
    feed.name: feed
    for feed in [
        Feed("dailies", 365, _fetch_dailies, _keyed_by_date("calendarDate")),
        Feed("sleeps", 31, _fetch_sleeps, _keyed_by_date("calendarDate")),
        Feed("hrv", 28, _fetch_hrv, _keyed_by_date("calendarDate")),
        Feed("stress", 28, _fetch_stress, _keyed_by_date("calendarDate")),
        Feed("bodyBattery", 31, _fetch_body_battery, _keyed_by_date("date")),
        Feed("weight", 31, _fetch_weight, _weight_key),
        Feed("activities", 31, _fetch_activities, _activity_key),
    ]
}


@dataclass(frozen=True)
class SyncContext:
    database_path: Path
    client: GarminClient
    clock: Clock


def chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into consecutive inclusive ranges of at most `max_days` days."""
    result = []
    while start <= end:
        chunk_end = min(start + timedelta(days=max_days - 1), end)
        result.append((start, chunk_end))
        start = chunk_end + timedelta(days=1)
    return result


def _now_iso(ctx: SyncContext) -> str:
    return datetime.fromtimestamp(ctx.clock(), UTC).isoformat()


async def sync_feed(ctx: SyncContext, feed: Feed, start: date, end: date) -> int:
    """Pull one feed for [start, end]; returns the number of records stored.

    Progress is recorded per chunk, so a failure part-way keeps what was pulled.
    """
    stored = 0
    for chunk_start, chunk_end in chunks(start, end, feed.max_days):
        records = await feed.fetch(ctx.client, chunk_start, chunk_end)
        received_at = _now_iso(ctx)
        with closing(db.connect(ctx.database_path)) as conn:
            for record in records:
                key = feed.key(record)
                if key is None:
                    log.warning("skipping %s record without a key", feed.name)
                    continue
                summary_id, calendar_date, start_time = key
                storage.upsert_summary(
                    conn, feed.name, summary_id, record, received_at, calendar_date, start_time
                )
                stored += 1
            storage.record_sync_success(conn, feed.name, chunk_end.isoformat(), received_at)
    return stored


async def run_sync(ctx: SyncContext, ranges: dict[str, tuple[date, date]]) -> dict[str, int | str]:
    """Sync each feed over its range. One feed failing doesn't stop the others."""
    results: dict[str, int | str] = {}
    for name, (start, end) in ranges.items():
        try:
            results[name] = await sync_feed(ctx, FEEDS[name], start, end)
        except (GarminAPIError, httpx.HTTPError) as exc:
            log.error("sync of %s failed: %s", name, exc)
            with closing(db.connect(ctx.database_path)) as conn:
                storage.record_sync_error(conn, name, str(exc), _now_iso(ctx))
            results[name] = f"error: {exc}"
    log.info("sync finished: %s", results)
    return results


def scheduled_ranges(
    state: dict[str, dict[str, Any]], today: date, lookback_days: int
) -> dict[str, tuple[date, date]]:
    # Tomorrow (UTC) so the user's current local day is included in any timezone.
    end = today + timedelta(days=1)
    earliest = today - timedelta(days=MAX_CATCH_UP_DAYS)
    ranges = {}
    for name in FEEDS:
        synced_through = (state.get(name) or {}).get("synced_through")
        if synced_through:
            start = date.fromisoformat(synced_through) - timedelta(days=lookback_days)
            start = min(start, today - timedelta(days=lookback_days))
        else:
            start = earliest
        ranges[name] = (max(start, earliest), end)
    return ranges


async def scheduled_sync(ctx: SyncContext, lookback_days: int) -> dict[str, int | str]:
    with closing(db.connect(ctx.database_path)) as conn:
        state = storage.sync_state(conn)
    today = datetime.fromtimestamp(ctx.clock(), UTC).date()
    return await run_sync(ctx, scheduled_ranges(state, today, lookback_days))


def warn_if_key_expiring(expires: date | None, today: date, warn_days: int = 14) -> None:
    if expires is None:
        return
    days_left = (expires - today).days
    if days_left < 0:
        log.error("GARMIN_API_KEY expired; create a new one in Garmin Connect")
    elif days_left <= warn_days:
        log.warning("GARMIN_API_KEY expires in %d days; rotate it soon", days_left)


# --- command line: pull a historical range ---


def parse_history_args(argv: list[str] | None) -> tuple[date, date, list[str]]:
    parser = argparse.ArgumentParser(
        prog="python -m garmin_reasoner.sync",
        description="Pull a historical date range from Garmin (e.g. on first setup).",
    )
    parser.add_argument("start", type=date.fromisoformat, help="first date, YYYY-MM-DD")
    parser.add_argument("end", type=date.fromisoformat, help="last date, YYYY-MM-DD")
    parser.add_argument(
        "--feeds",
        nargs="+",
        choices=sorted(FEEDS),
        metavar="FEED",
        help=f"only these feeds (default: all of {', '.join(FEEDS)})",
    )
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("end must not be before start")
    return args.start, args.end, args.feeds or list(FEEDS)


async def history_sync(
    settings: "Settings",
    start: date,
    end: date,
    feeds: list[str],
    http: httpx.AsyncClient | None = None,
    retry_base_delay: float = 5.0,
) -> dict[str, int | str]:
    async with AsyncExitStack() as stack:
        if http is None:
            http = await stack.enter_async_context(httpx.AsyncClient(timeout=60.0))
        client = GarminClient(
            http,
            settings.garmin_api_base_url,
            settings.garmin_api_key,
            retry_base_delay=retry_base_delay,
        )
        ctx = SyncContext(settings.database_path, client, time.time)
        return await run_sync(ctx, {name: (start, end) for name in feeds})


def main(argv: list[str] | None = None) -> int:
    """Run a history sync in the foreground. A command rather than an HTTP route, so
    there is no admin endpoint on the network to protect."""
    from garmin_reasoner.config import get_settings

    start, end, feeds = parse_history_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db.restrict_new_files()
    settings = get_settings()
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
    results = asyncio.run(history_sync(settings, start, end, feeds))
    return 1 if any(isinstance(result, str) for result in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
