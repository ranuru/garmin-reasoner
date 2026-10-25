"""Remote MCP server (streamable HTTP at `/mcp`) with read-only tools over stored data."""

from contextlib import closing
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.applications import Starlette

from garmin_reasoner import db, queries, storage
from garmin_reasoner.config import Settings
from garmin_reasoner.mcp_auth import SCOPE, SQLiteOAuthProvider

MAX_RANGE_DAYS = 93
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Garmin Connect health and activity data for one person, as stored by their own service.
Dates are the person's local calendar dates (YYYY-MM-DD), and ranges are inclusive.
Records are Garmin's raw JSON. Units differ by type and are usually in the field name
(...Seconds, ...InMillis, ...Meters). Call list_available_data first to see what exists
and how fresh it is.
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


def _register_tools(mcp: MCPServer, settings: Settings) -> None:
    database_path = settings.database_path

    def fetch(summary_type: str, start: date, end: date) -> dict[str, Any]:
        if end < start:
            raise ValueError("end must not be before start")
        if end - start > timedelta(days=MAX_RANGE_DAYS - 1):
            raise ValueError(f"range is limited to {MAX_RANGE_DAYS} days; split the request")
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
