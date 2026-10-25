"""FastAPI application factory."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from garmin_reasoner import backfill, backup, db, jobs, mcp_auth, mcp_server, oauth, webhook
from garmin_reasoner.config import Settings, get_settings


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    token_provider: oauth.TokenProvider | None = None,
    clock: oauth.Clock = time.time,
    backfill_delay: float = 0.5,
) -> FastAPI:
    """Build the app. The keyword arguments are injectable for tests.

    `token_provider` defaults to the stored-token manager, which refreshes as needed.
    """
    settings = settings or get_settings()

    provider = mcp_auth.SQLiteOAuthProvider(
        settings.database_path,
        settings.public_base_url,
        settings.mcp_allowed_redirect_uris,
        clock,
    )
    mcp = mcp_server.build_mcp_server(settings, provider)
    mcp_app = mcp_server.build_mcp_app(mcp, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = db.connect(settings.database_path)
        db.init_db(conn)
        conn.close()

        async with AsyncExitStack() as stack:
            http = http_client or await stack.enter_async_context(httpx.AsyncClient(timeout=30.0))
            garmin = oauth.GarminClient(settings, http, clock)
            tokens = oauth.TokenManager(settings.database_path, garmin, clock)
            provide_token = token_provider or tokens.get_access_token
            app.state.oauth = oauth.OAuthContext(
                database_path=settings.database_path, garmin=garmin, tokens=tokens, clock=clock
            )
            app.state.webhook = webhook.WebhookContext(
                database_path=settings.database_path, http=http, token_provider=provide_token
            )
            app.state.backfill = backfill.BackfillContext(
                database_path=settings.database_path,
                garmin=garmin,
                token_provider=provide_token,
                clock=clock,
                request_delay=backfill_delay,
            )
            # The mounted MCP app's own lifespan doesn't run, so start its sessions here.
            await stack.enter_async_context(mcp.session_manager.run())

            if settings.jobs_enabled:
                task = asyncio.create_task(
                    jobs.run_periodically(
                        {
                            "gap-check": lambda: backfill.run_gap_check(
                                app.state.backfill, settings.gap_check_days
                            ),
                            "backup": lambda: asyncio.to_thread(
                                backup.backup_database,
                                settings.database_path,
                                settings.backup_dir,
                                settings.backup_keep,
                                datetime.now(UTC),
                            ),
                        }
                    )
                )
                stack.callback(task.cancel)

            yield

    app = FastAPI(title="garmin-reasoner", lifespan=lifespan)
    app.state.settings = settings
    app.state.mcp_provider = provider
    app.state.login_throttle = mcp_auth.LoginThrottle(clock=clock)
    app.include_router(webhook.router)
    app.include_router(oauth.router)
    app.include_router(backfill.router)
    app.include_router(mcp_auth.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Last, so FastAPI's own routes win: serves /mcp plus the OAuth endpoints
    # (/authorize, /token, /register, /revoke, /.well-known/...).
    app.mount("/", mcp_app)
    return app
