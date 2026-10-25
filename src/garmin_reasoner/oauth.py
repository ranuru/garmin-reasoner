"""Garmin OAuth 2.0 + PKCE: authorization flow, token refresh, and disconnect.

Flow: `/connect` stores a random state + PKCE verifier and redirects to Garmin. Garmin
redirects back to `/callback` with a code, which we exchange (with the verifier) for
tokens, then look up the Garmin user ID and store both.

Tokens are never logged.
"""

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from garmin_reasoner import db, storage
from garmin_reasoner.auth import require_admin
from garmin_reasoner.config import Settings
from garmin_reasoner.storage import TokenSet

log = logging.getLogger(__name__)

WELLNESS_API = "https://apis.garmin.com/wellness-api/rest"
STATE_TTL_SECONDS = 600
# Refresh this long before the access token expires, so it can't expire mid-request.
REFRESH_MARGIN_SECONDS = 300

Clock = Callable[[], float]

# Given a Garmin user ID, return a valid access token for calling Garmin's API.
TokenProvider = Callable[[str], Awaitable[str]]


class NoAccessTokenError(Exception):
    pass


class OAuthError(Exception):
    pass


def make_code_verifier() -> str:
    # 64 random bytes -> 86 URL-safe chars; RFC 7636 allows 43-128.
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GarminClient:
    """Thin client for Garmin's OAuth and user-registration endpoints."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient, clock: Clock) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock

    def authorization_url(self, state: str, verifier: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._settings.garmin_client_id,
            "redirect_uri": self._settings.redirect_uri,
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{self._settings.garmin_authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str, verifier: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self._settings.redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            previous_refresh_token=refresh_token,
        )

    async def fetch_user_id(self, access_token: str) -> str:
        response = await self._http.get(
            f"{WELLNESS_API}/user/id", headers={"Authorization": f"Bearer {access_token}"}
        )
        response.raise_for_status()
        try:
            return str(response.json()["userId"])
        except (ValueError, KeyError, TypeError) as exc:
            raise OAuthError("unexpected response from user/id endpoint") from exc

    async def delete_registration(self, access_token: str) -> None:
        response = await self._http.delete(
            f"{WELLNESS_API}/user/registration",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()

    async def _token_request(
        self, data: dict[str, str], previous_refresh_token: str | None = None
    ) -> TokenSet:
        credentials = {
            "client_id": self._settings.garmin_client_id,
            "client_secret": self._settings.garmin_client_secret.get_secret_value(),
        }
        response = await self._http.post(self._settings.garmin_token_url, data=data | credentials)
        if response.status_code != 200:
            # The body may say why (e.g. invalid_grant); it doesn't contain tokens.
            raise OAuthError(f"token endpoint returned {response.status_code}: {response.text}")

        try:
            payload: dict[str, Any] = response.json()
            now = int(self._clock())
            refresh_expires_in = payload.get("refresh_token_expires_in")
            # Garmin rotates refresh tokens; keep the old one only if no new one came back.
            refresh_token = payload.get("refresh_token") or previous_refresh_token
            if not refresh_token:
                raise OAuthError("token response had no refresh_token")
            return TokenSet(
                access_token=payload["access_token"],
                refresh_token=refresh_token,
                access_expires_at=now + int(payload["expires_in"]),
                refresh_expires_at=now + int(refresh_expires_in) if refresh_expires_in else None,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise OAuthError("unexpected response from token endpoint") from exc


class TokenManager:
    """Hands out valid access tokens, refreshing them shortly before they expire."""

    def __init__(self, database_path: Path, garmin: GarminClient, clock: Clock) -> None:
        self._database_path = database_path
        self._garmin = garmin
        self._clock = clock
        # Refresh tokens rotate, so two concurrent refreshes would race and one would fail
        # with an already-used token. Serialize per user.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get_access_token(self, user_id: str) -> str:
        async with self._locks[user_id]:
            with closing(db.connect(self._database_path)) as conn:
                tokens = storage.load_tokens(conn, user_id)
                if tokens is None:
                    raise NoAccessTokenError(f"no tokens stored for user {user_id}")
                if tokens.access_expires_at - REFRESH_MARGIN_SECONDS > self._clock():
                    return tokens.access_token

                try:
                    tokens = await self._garmin.refresh(tokens.refresh_token)
                except (OAuthError, httpx.HTTPError) as exc:
                    raise NoAccessTokenError(f"token refresh failed for user {user_id}") from exc
                storage.save_tokens(conn, user_id, tokens, updated_at=_now_iso())
                log.info("refreshed access token for user %r", user_id)
                return tokens.access_token


@dataclass(frozen=True)
class OAuthContext:
    database_path: Path
    garmin: GarminClient
    tokens: TokenManager
    clock: Clock = time.time


router = APIRouter()


@router.get("/connect", dependencies=[Depends(require_admin)])
def connect(request: Request) -> RedirectResponse:
    ctx: OAuthContext = request.app.state.oauth
    state = secrets.token_urlsafe(32)
    verifier = make_code_verifier()
    with closing(db.connect(ctx.database_path)) as conn:
        storage.save_oauth_state(conn, state, verifier, created_at=int(ctx.clock()))
    return RedirectResponse(ctx.garmin.authorization_url(state, verifier), status_code=302)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> dict[str, str]:
    ctx: OAuthContext = request.app.state.oauth
    if error:
        raise HTTPException(status_code=400, detail=f"Garmin authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    with closing(db.connect(ctx.database_path)) as conn:
        verifier = storage.pop_oauth_state(
            conn, state, not_before=int(ctx.clock()) - STATE_TTL_SECONDS
        )
        if verifier is None:
            raise HTTPException(status_code=400, detail="unknown or expired state")

        try:
            tokens = await ctx.garmin.exchange_code(code, verifier)
            user_id = await ctx.garmin.fetch_user_id(tokens.access_token)
        except (OAuthError, httpx.HTTPError) as exc:
            log.exception("OAuth callback failed")
            raise HTTPException(
                status_code=502, detail="token exchange with Garmin failed"
            ) from exc

        now = _now_iso()
        storage.add_user(conn, user_id, connected_at=now)
        storage.save_tokens(conn, user_id, tokens, updated_at=now)

    log.info("connected Garmin user %r", user_id)
    return {"status": "connected", "user_id": user_id}


@router.post("/admin/disconnect", dependencies=[Depends(require_admin)])
async def disconnect(request: Request) -> dict[str, list[str]]:
    """Deregister every connected user with Garmin, then forget them locally.

    Stored summaries are kept. If Garmin rejects the call the user stays connected, so
    the request can be retried.
    """
    ctx: OAuthContext = request.app.state.oauth
    with closing(db.connect(ctx.database_path)) as conn:
        user_ids = sorted(storage.known_user_ids(conn))

    disconnected: list[str] = []
    for user_id in user_ids:
        try:
            await ctx.garmin.delete_registration(await ctx.tokens.get_access_token(user_id))
        except NoAccessTokenError:
            log.warning("no valid token for %r; removing locally only", user_id)
        except httpx.HTTPError as exc:
            log.exception("Garmin deregistration failed for %r", user_id)
            raise HTTPException(
                status_code=502, detail="deregistration with Garmin failed"
            ) from exc

        with closing(db.connect(ctx.database_path)) as conn:
            storage.delete_user(conn, user_id)
        disconnected.append(user_id)

    return {"disconnected": disconnected}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
