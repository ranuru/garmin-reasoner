"""Remote MCP server (streamable HTTP at `/mcp`) with read-only tools over stored data."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import TextContent, ToolAnnotations
from starlette.applications import Starlette

from garmin_reasoner import compact, db, queries, storage
from garmin_reasoner.compact import GroupBy
from garmin_reasoner.config import Settings
from garmin_reasoner.mcp_auth import SCOPE, SQLiteOAuthProvider

MAX_RANGE_DAYS = 93
# Monthly and whole-range stats have no limit: they're clipped to the stored history,
# and a month row is ~200 tokens.
MAX_WEEKLY_STATS_RANGE_DAYS = 366
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Garmin Connect health and activity data for one person, as stored by their own service.
Dates are the person's local calendar dates (YYYY-MM-DD), and ranges are inclusive.
Call list_available_data first to see what exists and how fresh it is.

Start with the compact summary tools: get_period_stats for trends and comparisons
across weeks or months, then get_sleep_summary, get_daily_overview and
get_activity_list for one row per night, day or activity. They return flat rows with
units in the key names and null for values that weren't recorded.

The get_sleep, get_daily_summaries, get_activities, get_hrv, get_stress,
get_body_battery and get_weight tools return Garmin's raw JSON, which is large. Use
them only to drill into a few days or one activity for a detail the summaries lack.
Units in raw records differ by type and are usually in the field name (...Seconds,
...InMillis, ...Meters).
"""


def build_mcp_server(settings: Settings, provider: SQLiteOAuthProvider) -> MCPServer:
    base = settings.public_base_url
    mcp = MCPServer(
        name="garmin-reasoner",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base,
            resource_server_url=f"{base}/mcp",
            # We are the only issuer and only resource, so every token we issue is for /mcp.
            validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    _register_tools(mcp, settings)
    return mcp


def public_origin(public_base_url: str) -> str:
    """scheme://host[:port] of the public URL; Origin headers never carry a path."""
    url = urlsplit(public_base_url)
    return f"{url.scheme}://{url.netloc}"


def allowed_hosts(public_base_url: str) -> list[str]:
    """Host header values /mcp accepts (DNS rebinding protection).

    With a non-default port (e.g. Funnel on 10000), proxies may send Host with or
    without it; accept both, but nothing else.
    """
    url = urlsplit(public_base_url)
    return sorted({url.netloc, url.hostname or url.netloc})


def build_mcp_app(mcp: MCPServer, settings: Settings) -> Starlette:
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless + JSON: no server-side sessions to lose on restart, no SSE to proxy.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts(settings.public_base_url),
            allowed_origins=[
                public_origin(settings.public_base_url),
                "https://claude.ai",
                "https://claude.com",
            ],
        ),
    )


def _check_range(start: date, end: date, max_days: int | None) -> None:
    if end < start:
        raise ValueError("end must not be before start")
    if max_days is not None and end - start > timedelta(days=max_days - 1):
        raise ValueError(f"range is limited to {max_days} days; split the request")


def _register_tools(mcp: MCPServer, settings: Settings) -> None:
    database_path = settings.database_path

    def compact_view(
        view: Callable[[sqlite3.Connection, date, date], dict[str, Any]],
        start: date,
        end: date,
        max_days: int | None = MAX_RANGE_DAYS,
    ) -> TextContent:
        _check_range(start, end, max_days)
        with closing(db.connect(database_path)) as conn:
            result = view(conn, start, end)
        return TextContent(type="text", text=compact.to_text(result))

    def fetch(summary_type: str, start: date, end: date) -> dict[str, Any]:
        _check_range(start, end, MAX_RANGE_DAYS)
        with closing(db.connect(database_path)) as conn:
            summaries = queries.summaries_in_range(conn, summary_type, start, end)
        return {
            "type": summary_type,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(summaries),
            "summaries": summaries,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_available_data() -> dict[str, Any]:
        """Which data types are stored (with counts and first/last dates), and when each
        was last synced from Garmin. Stale sync times mean recent data may be missing."""
        with closing(db.connect(database_path)) as conn:
            overview = queries.data_overview(conn)
            sync = storage.sync_state(conn)
        expires = settings.garmin_api_key_expires
        return {
            "types": overview,
            "sync": sync,
            "garmin_api_key_expires": expires.isoformat() if expires else None,
        }

    # --- compact summaries: use these first ---

    @mcp.tool(annotations=READ_ONLY)
    def get_period_stats(start: date, end: date, group_by: GroupBy = "week") -> TextContent:
        """Start here for trends, such as "how has my sleep been the last 3 months?".
        Pre-computed stats per ISO week (up to 366 days), or per calendar month or for
        the whole range (group_by "none") over any range, including all history: pass
        a start date early enough, e.g. 2000-01-01. The range is clipped to the
        dates that have data, and the returned start and end show what was covered.
        Each row: period, start, end, days, nights with sleep data; avg and median of
        sleep_min, sleep_score, bedtime (local HH:MM), resting_hr, hrv (overnight ms),
        steps and stress (0-100); nights_under_7h and nights_under_6h; activities as
        {type: count}; im_moderate and im_vigorous totals, and im_total = moderate +
        2 x vigorous (Garmin's intensity-minute goal); weight_first_kg and
        weight_last_kg. Partial periods at either end are clipped (see days). Today is
        usually incomplete."""
        max_days = MAX_WEEKLY_STATS_RANGE_DAYS if group_by == "week" else None
        return compact_view(
            lambda conn, s, e: compact.period_stats(conn, s, e, group_by), start, end, max_days
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_sleep_summary(start: date, end: date) -> TextContent:
        """One compact row per night, dated by the morning it ends (up to 93 days).
        Fields: date, bedtime and wake (local HH:MM), sleep_min, deep_min, light_min,
        rem_min, awake_min, score (0-100), avg_stress (during sleep), avg_hr (bpm),
        avg_resp (breaths/min), hrv (overnight average, ms), nap_min. Dates with
        neither sleep nor HRV data are listed in "missing". Use get_sleep only for
        details not here (SpO2, sleep need, sub-scores)."""
        return compact_view(compact.sleep_summary, start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_daily_overview(start: date, end: date) -> TextContent:
        """One compact row per day (up to 93 days): date, steps, floors (climbed),
        resting_hr, im_moderate and im_vigorous (intensity minutes), active_kcal,
        bb_max and bb_min (Body Battery), stress (average 0-100), and activities as
        "type minutes" strings. Days without data are listed in "missing". Today is
        usually incomplete. Use get_daily_summaries only for details not here."""
        return compact_view(compact.daily_overview, start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_activity_list(start: date, end: date) -> TextContent:
        """One compact row per recorded activity (up to 93 days): start (local
        "YYYY-MM-DD HH:MM"), type, name, duration_min, distance_km, pace_min_km
        ("m:ss" per km, runs only) or speed_kmh (other activities with a distance),
        avg_hr, max_hr, elev_gain_m, training_effect (Garmin's label), z1_min..z5_min
        (minutes in each heart rate zone). Use get_activities only for details not
        here (power, cadence, running dynamics, splits)."""
        return compact_view(compact.activity_list, start, end)

    # --- raw Garmin JSON: for drill-down ---

    @mcp.tool(annotations=READ_ONLY)
    def get_daily_summaries(start: date, end: date) -> dict[str, Any]:
        """Daily summaries: steps, floors, calories, heart rate (min/max/resting),
        intensity minutes, stress, Body Battery, hydration, respiration and pulse ox.
        Durations in these summaries are milliseconds."""
        return fetch("dailies", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_sleep(start: date, end: date) -> dict[str, Any]:
        """Nightly sleep: total/deep/light/REM/awake seconds, sleep scores, SpO2,
        respiration, sleep stress, sleep need and naps. Dated by the morning it ends."""
        return fetch("sleeps", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_hrv(start: date, end: date) -> dict[str, Any]:
        """Overnight heart rate variability: last-night average, 5-minute high, weekly
        average, personal baseline range and status."""
        return fetch("hrv", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_activities(start: date, end: date) -> dict[str, Any]:
        """Recorded activities (runs, rides, workouts, ...): type, duration (seconds),
        distance (meters), heart rate, pace/speed, power, training effect, calories.
        Dated by local start time. GPS coordinates are omitted."""
        return fetch("activities", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_stress(start: date, end: date) -> dict[str, Any]:
        """Daily stress: overall level (0-100) and seconds in high/medium/low/rest stress."""
        return fetch("stress", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_body_battery(start: date, end: date) -> dict[str, Any]:
        """Daily Body Battery reports: charged/drained amounts, the intraday level
        timeline, and events (sleep, activities, stress) that moved it."""
        return fetch("bodyBattery", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_weight(start: date, end: date) -> dict[str, Any]:
        """Weigh-ins: weight and muscle/bone mass in GRAMS (divide by 1000 for kg), BMI,
        body fat and water percentages."""
        return fetch("weight", start, end)
