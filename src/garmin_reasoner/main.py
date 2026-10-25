"""FastAPI application factory."""

import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI

from garmin_reasoner import db, oauth, webhook
from garmin_reasoner.config import Settings, get_settings


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    token_provider: oauth.TokenProvider | None = None,
    clock: oauth.Clock = time.time,
) -> FastAPI:
    """Build the app. The keyword arguments are injectable for tests.

    `token_provider` defaults to the stored-token manager, which refreshes as needed.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = db.connect(settings.database_path)
        db.init_db(conn)
        conn.close()

        async with AsyncExitStack() as stack:
            http = http_client or await stack.enter_async_context(httpx.AsyncClient(timeout=30.0))
            garmin = oauth.GarminClient(settings, http, clock)
            tokens = oauth.TokenManager(settings.database_path, garmin, clock)
            app.state.oauth = oauth.OAuthContext(
                database_path=settings.database_path, garmin=garmin, tokens=tokens, clock=clock
            )
            app.state.webhook = webhook.WebhookContext(
                database_path=settings.database_path,
                http=http,
                token_provider=token_provider or tokens.get_access_token,
            )
            yield

    app = FastAPI(title="garmin-reasoner", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(webhook.router)
    app.include_router(oauth.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
