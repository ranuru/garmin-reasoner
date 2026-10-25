"""Garmin push/ping notification receiver.

Garmin POSTs a JSON object keyed by summary type, each holding a list of entries:

- push: each entry is a full summary (has `summaryId`).
- ping: each entry has a `callbackURL` we GET (with the user's bearer token) to fetch
  a list of summaries.

The endpoint only parses the body and responds 200; storing and fetching happen in a
background task so Garmin never waits on us.
"""

import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from garmin_reasoner import db, storage
from garmin_reasoner.oauth import NoAccessTokenError, TokenProvider

log = logging.getLogger(__name__)

GARMIN_API_HOST = "apis.garmin.com"


@dataclass(frozen=True)
class WebhookContext:
    database_path: Path
    http: httpx.AsyncClient
    token_provider: TokenProvider


router = APIRouter()


@router.post("/garmin/webhook")
async def garmin_webhook(request: Request, background: BackgroundTasks) -> Response:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    background.add_task(process_notification, request.app.state.webhook, body)
    return Response(status_code=200)


async def process_notification(ctx: WebhookContext, body: dict[str, Any]) -> None:
    received_at = datetime.now(UTC).isoformat()
    with closing(db.connect(ctx.database_path)) as conn:
        known_users = storage.known_user_ids(conn)

        for summary_type, entries in body.items():
            if not isinstance(entries, list):
                log.warning("ignoring %r: expected a list of entries", summary_type)
                continue

            for entry in entries:
                if not isinstance(entry, dict):
                    log.warning("ignoring non-object entry in %r", summary_type)
                    continue

                user_id = entry.get("userId")
                if user_id not in known_users:
                    log.warning("dropping %r entry for unknown user %r", summary_type, user_id)
                    continue

                if summary_type == "deregistrations":
                    storage.delete_user(conn, user_id)
                    known_users.discard(user_id)
                    log.info("user %r deregistered via Garmin", user_id)
                    continue

                if "callbackURL" in entry:
                    summaries = await fetch_ping(ctx, summary_type, entry)
                elif "summaryId" in entry:
                    summaries = [entry]
                else:
                    # e.g. userPermissionsChange; not handled yet.
                    log.info("unhandled %r notification for user %r", summary_type, user_id)
                    continue

                for summary in summaries:
                    if not isinstance(summary, dict) or "summaryId" not in summary:
                        log.warning("skipping %r item without summaryId", summary_type)
                        continue
                    storage.upsert_summary(
                        conn,
                        summary_type,
                        summary,
                        user_id=summary.get("userId", user_id),
                        received_at=received_at,
                    )


async def fetch_ping(ctx: WebhookContext, summary_type: str, entry: dict[str, Any]) -> list[Any]:
    """Fetch the summaries a ping points at. Returns [] (and logs) on any failure."""
    url = entry["callbackURL"]
    if not is_garmin_api_url(url):
        # Never send a bearer token to a host named in an unauthenticated request.
        log.warning("refusing %r callbackURL outside %s: %r", summary_type, GARMIN_API_HOST, url)
        return []

    try:
        token = await ctx.token_provider(entry["userId"])
        response = await ctx.http.get(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()
    except (NoAccessTokenError, httpx.HTTPError, ValueError):
        log.exception("failed to fetch %r ping callback", summary_type)
        return []

    if not isinstance(data, list):
        log.warning("%r ping callback returned %s, expected a list", summary_type, type(data))
        return []
    return data


def is_garmin_api_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == GARMIN_API_HOST and parts.port is None
