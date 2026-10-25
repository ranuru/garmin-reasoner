"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI

from garmin_reasoner import db, webhook
from garmin_reasoner.config import Settings, get_settings


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    token_provider: webhook.TokenProvider = webhook.no_access_token,
) -> FastAPI:
    """Build the app. `http_client` and `token_provider` are injectable for tests."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = db.connect(settings.database_path)
        db.init_db(conn)
        conn.close()

        async with AsyncExitStack() as stack:
            http = http_client or await stack.enter_async_context(httpx.AsyncClient(timeout=30.0))
            app.state.webhook = webhook.WebhookContext(
                database_path=settings.database_path,
                http=http,
                token_provider=token_provider,
            )
            yield

    app = FastAPI(title="garmin-reasoner", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(webhook.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
