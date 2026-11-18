"""FastAPI application factory."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from garmin_reasoner import backup, db, garmin, jobs, mcp_auth, mcp_server, security, sync
from garmin_reasoner.config import Settings, get_settings

BACKUP_INTERVAL_SECONDS = 24 * 3600


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    clock: garmin.Clock = time.time,
    retry_base_delay: float = 5.0,
) -> FastAPI:
    """Build the app. The keyword arguments are injectable for tests."""
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
        db.restrict_new_files()
        db.ensure_writable_dir(settings.database_path.parent)
        if settings.jobs_enabled:
            db.ensure_writable_dir(settings.backup_dir)
        conn = db.connect(settings.database_path)
        db.init_db(conn)
        conn.close()
        # Also tighten files left world-readable by older versions.
        db.restrict_permissions(settings.database_path.parent)
        if settings.jobs_enabled:
            db.restrict_permissions(settings.backup_dir)

        async with AsyncExitStack() as stack:
            http = http_client or await stack.enter_async_context(httpx.AsyncClient(timeout=60.0))
            client = garmin.GarminClient(
                http,
                settings.garmin_api_base_url,
                settings.garmin_api_key,
                clock,
                retry_base_delay=retry_base_delay,
            )
            app.state.sync = sync.SyncContext(settings.database_path, client, clock)
            # The mounted MCP app's own lifespan doesn't run, so start its sessions here.
            await stack.enter_async_context(mcp.session_manager.run())

            if settings.jobs_enabled:
                start_jobs(app, settings, stack)
            yield

    # No /docs, /redoc or /openapi.json: they would map the app for anyone who finds it.
    app = FastAPI(
        title="garmin-reasoner", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.mcp_provider = provider
    app.state.login_throttle = mcp_auth.LoginThrottle(clock=clock)
    # The last one added runs first: headers on every response, then the body limit,
    # then stripping PUBLIC_BASE_URL's path (e.g. /garmin), which also keeps the public
    # internet away from everything but the MCP and OAuth routes.
    app.add_middleware(mcp_auth.PathPrefix, prefix=settings.public_path_prefix)
    app.add_middleware(security.BodySizeLimit)
    app.add_middleware(security.SecurityHeaders)
    app.include_router(mcp_auth.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Last, so FastAPI's own routes win: serves /mcp plus the OAuth endpoints
    # (/authorize, /token, /register, /revoke, /.well-known/...).
    app.mount("/", mcp_auth.BasicAuthForPostClients(mcp_app, provider))
    return app


def start_jobs(app: FastAPI, settings: Settings, stack: AsyncExitStack) -> None:
    async def sync_job() -> None:
        sync.warn_if_key_expiring(settings.garmin_api_key_expires, datetime.now(UTC).date())
        await sync.scheduled_sync(app.state.sync, settings.sync_lookback_days)

    async def backup_job() -> None:
        await asyncio.to_thread(
            backup.backup_database,
            settings.database_path,
            settings.backup_dir,
            settings.backup_keep,
            datetime.now(UTC),
        )

    tasks = [
        asyncio.create_task(
            jobs.run_periodically(
                "sync", sync_job, settings.sync_interval_minutes * 60, initial_delay=10
            )
        ),
        asyncio.create_task(
            jobs.run_periodically("backup", backup_job, BACKUP_INTERVAL_SECONDS, initial_delay=300)
        ),
    ]
    for task in tasks:
        stack.callback(task.cancel)
