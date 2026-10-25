"""Remote MCP server (streamable HTTP at `/mcp`) with read-only tools over stored data."""

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.applications import Starlette

from garmin_reasoner import db, queries
from garmin_reasoner.config import Settings
from garmin_reasoner.mcp_auth import SCOPE, SQLiteOAuthProvider

MAX_RANGE_DAYS = 93
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Garmin Connect health and activity data for one person, as stored by their own service.
Dates are the person's local calendar dates (YYYY-MM-DD), and ranges are inclusive.
Summaries are Garmin's raw JSON: durations are in seconds, timestamps are epoch seconds
(UTC) with a separate local offset. Call list_available_data first to see what exists.
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
    _register_tools(mcp, settings.database_path)
    return mcp


def build_mcp_app(mcp: MCPServer, settings: Settings) -> Starlette:
    host = urlsplit(settings.public_base_url).netloc
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless + JSON: no server-side sessions to lose on restart, no SSE to proxy.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host],
            allowed_origins=[settings.public_base_url, "https://claude.ai", "https://claude.com"],
        ),
    )


def _register_tools(mcp: MCPServer, database_path: Path) -> None:
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
        """Which summary types are stored, with counts and first/last local dates."""
        with closing(db.connect(database_path)) as conn:
            return {"types": queries.data_overview(conn)}

    @mcp.tool(annotations=READ_ONLY)
    def get_daily_summaries(start: date, end: date) -> dict[str, Any]:
        """Daily summaries per day: steps, distance, calories, resting/avg/max heart rate,
        intensity minutes, body battery and stress averages."""
        return fetch("dailies", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_sleep(start: date, end: date) -> dict[str, Any]:
        """Sleep summaries: duration, deep/light/REM/awake seconds, sleep score, and
        stage timelines. A night is dated by the morning it ends."""
        return fetch("sleeps", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_hrv(start: date, end: date) -> dict[str, Any]:
        """Overnight heart rate variability: last-night average and 5-minute values."""
        return fetch("hrv", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_activities(start: date, end: date) -> dict[str, Any]:
        """Recorded activities (runs, rides, workouts, ...): type, duration, distance,
        heart rate, pace/speed, calories. Dated by local start time."""
        return fetch("activities", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_stress(start: date, end: date) -> dict[str, Any]:
        """Stress details per day, including time-offset stress and body battery values."""
        return fetch("stressDetails", start, end)

    @mcp.tool(annotations=READ_ONLY)
    def get_summaries(summary_type: str, start: date, end: date) -> dict[str, Any]:
        """Any stored summary type by name (see list_available_data), e.g. bodyComps,
        pulseox, allDayRespiration, userMetrics, skinTemp."""
        return fetch(summary_type, start, end)
