"""Historical backfill (`POST /admin/backfill`) and the gap-check that requests it.

Garmin answers a backfill request with 202 and later delivers the data to the webhook
like any other notification. Each request covers at most 24 hours, so ranges are split
into one request per day per summary type.
"""

import asyncio
import logging
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from garmin_reasoner import db, queries, storage
from garmin_reasoner.auth import require_admin
from garmin_reasoner.oauth import Clock, GarminClient, NoAccessTokenError, TokenProvider

log = logging.getLogger(__name__)

# Backfill endpoint names (`/wellness-api/rest/backfill/<type>`). Verify against the
# portal docs: some differ from the webhook's summary-type keys.
BACKFILL_TYPES = frozenset(
    {
        "activities",
        "activityDetails",
        "bloodPressures",
        "bodyComps",
        "dailies",
        "epochs",
        "healthSnapshot",
        "hrv",
        "moveiq",
        "pulseox",
        "respiration",
        "skinTemp",
        "sleeps",
        "stressDetails",
        "userMetrics",
    }
)
MAX_BACKFILL_DAYS = 90

# Types expected every day the watch is worn, so a missing day means a missed notification.
GAP_CHECK_TYPES = ("dailies", "sleeps", "hrv", "stressDetails")
# Don't treat the most recent days as gaps; their data may simply not be uploaded yet.
GAP_CHECK_SETTLE_DAYS = 2
GAP_RETRY_SECONDS = 7 * 24 * 3600
# After this many unanswered requests, assume there's no data (e.g. watch not worn).
MAX_GAP_ATTEMPTS = 3

Outcome = Literal["accepted", "duplicate", "failed"]


@dataclass(frozen=True)
class BackfillContext:
    database_path: Path
    garmin: GarminClient
    token_provider: TokenProvider
    clock: Clock
    # Pause between requests to stay well inside Garmin's rate limits.
    request_delay: float = 0.5


def _utc_midnight(day: date) -> int:
    return int(datetime.combine(day, time.min, UTC).timestamp())


def day_windows(start: date, end: date) -> list[tuple[int, int]]:
    """Consecutive 24h UTC windows covering every day in [start, end]."""
    days = (end - start).days + 1
    return [
        (_utc_midnight(start) + i * 86400, _utc_midnight(start) + (i + 1) * 86400)
        for i in range(days)
    ]


def gap_window(day: date) -> tuple[int, int]:
    """A 24h window centred on UTC midnight of `day`.

    A local day starts at UTC midnight minus the UTC offset, so this catches the summary
    starting on `day` for any offset within ±12h (and sleep that starts the evening before).
    """
    midnight = _utc_midnight(day)
    return midnight - 12 * 3600, midnight + 12 * 3600


async def _request(
    ctx: BackfillContext, user_id: str, summary_type: str, window: tuple[int, int]
) -> Outcome:
    try:
        token = await ctx.token_provider(user_id)
        status = await ctx.garmin.request_backfill(token, summary_type, *window)
    except (NoAccessTokenError, httpx.HTTPError):
        log.exception("backfill request failed: %s %s", summary_type, window)
        return "failed"
    finally:
        await asyncio.sleep(ctx.request_delay)
    return "duplicate" if status == 409 else "accepted"


def _known_users(ctx: BackfillContext) -> list[str]:
    with closing(db.connect(ctx.database_path)) as conn:
        return sorted(storage.known_user_ids(conn))


async def run_backfill(
    ctx: BackfillContext, summary_types: list[str], start: date, end: date
) -> Counter[Outcome]:
    outcomes: Counter[Outcome] = Counter()
    for user_id in _known_users(ctx):
        for summary_type in summary_types:
            for window in day_windows(start, end):
                outcomes[await _request(ctx, user_id, summary_type, window)] += 1
    log.info("backfill %s %s..%s finished: %s", summary_types, start, end, dict(outcomes))
    return outcomes


async def run_gap_check(ctx: BackfillContext, days: int) -> list[dict[str, str]]:
    """Request backfill for recent days that are missing a daily summary type."""
    today = datetime.fromtimestamp(ctx.clock(), UTC).date()
    end = today - timedelta(days=GAP_CHECK_SETTLE_DAYS)
    start = end - timedelta(days=days - 1)
    requested: list[dict[str, str]] = []

    for user_id in _known_users(ctx):
        for summary_type in GAP_CHECK_TYPES:
            with closing(db.connect(ctx.database_path)) as conn:
                present = queries.present_dates(conn, user_id, summary_type, start, end)
                missing = []
                for offset in range(days):
                    day = start + timedelta(days=offset)
                    if day in present:
                        continue
                    previous = storage.backfill_attempt(
                        conn, user_id, summary_type, day.isoformat()
                    )
                    if previous:
                        attempts, last_requested_at = previous
                        if attempts >= MAX_GAP_ATTEMPTS:
                            continue
                        if ctx.clock() - last_requested_at < GAP_RETRY_SECONDS:
                            continue
                    missing.append(day)

            for day in missing:
                if await _request(ctx, user_id, summary_type, gap_window(day)) == "failed":
                    continue
                with closing(db.connect(ctx.database_path)) as conn:
                    storage.record_backfill_attempt(
                        conn, user_id, summary_type, day.isoformat(), at=int(ctx.clock())
                    )
                requested.append({"type": summary_type, "date": day.isoformat()})

    log.info("gap-check requested %d backfills", len(requested))
    return requested


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class BackfillBody(BaseModel):
    summary_types: list[str]
    start: date
    end: date


@router.post("/backfill", status_code=202)
async def backfill(
    body: BackfillBody, request: Request, background: BackgroundTasks
) -> dict[str, int]:
    unknown = sorted(set(body.summary_types) - BACKFILL_TYPES)
    if not body.summary_types or unknown:
        raise HTTPException(
            422, f"unknown summary types: {unknown}; allowed: {sorted(BACKFILL_TYPES)}"
        )
    if body.end < body.start:
        raise HTTPException(422, "end must not be before start")
    days = (body.end - body.start).days + 1
    if days > MAX_BACKFILL_DAYS:
        raise HTTPException(422, f"at most {MAX_BACKFILL_DAYS} days per request")

    ctx: BackfillContext = request.app.state.backfill
    background.add_task(run_backfill, ctx, body.summary_types, body.start, body.end)
    return {"requests": days * len(body.summary_types) * len(_known_users(ctx))}


@router.post("/gap-check")
async def gap_check(request: Request) -> dict[str, list[dict[str, str]]]:
    ctx: BackfillContext = request.app.state.backfill
    return {"requested": await run_gap_check(ctx, request.app.state.settings.gap_check_days)}
