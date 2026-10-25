"""Client for the Garmin Connect Personal API (spec: docs/garmin-personal-api.json).

Auth: the personal API key is exchanged for a short-lived access token
(`POST .../personal-api-key/token` with `Authorization: ApiKey <key>`). Garmin returns
the token as a bare JWT in a text/plain body, valid 24h per its `exp` claim, and it is
sent as a bearer token. There are no refresh tokens; we just exchange the key again.

Neither the key nor tokens are ever logged or put in error messages.
"""

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr

log = logging.getLogger(__name__)

TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
# Garmin documents tokens as lasting 24h; used if the response doesn't say.
DEFAULT_TOKEN_TTL_SECONDS = 24 * 3600
# Re-exchange this long before expiry, so a token can't lapse mid-sync.
TOKEN_REFRESH_MARGIN_SECONDS = 300
MAX_RETRIES = 3
# What a bare token may look like (JWT / base64url). Anything else, e.g. an HTML error
# page served with 200, is rejected instead of being sent on as a bearer token.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-.~+/=]+")

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class GarminAPIError(Exception):
    pass


def _first(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if payload.get(name) is not None:
            return payload[name]
    return None


def _jwt_expiry(token: str) -> float | None:
    """The `exp` claim of a JWT, or None if the token isn't one.

    The signature isn't checked: the value only decides when to fetch a new token, and
    Garmin validates the token itself on every request.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return float(claims["exp"])
    except (ValueError, KeyError, TypeError):
        return None


class GarminClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: SecretStr,
        clock: Clock = time.time,
        retry_base_delay: float = 5.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._clock = clock
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _access_token(self, force_new: bool = False) -> str:
        async with self._token_lock:
            fresh = self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS > self._clock()
            if self._token and fresh and not force_new:
                return self._token

            response = await self._http.post(
                self._base_url + TOKEN_PATH,
                headers={"Authorization": f"ApiKey {self._api_key.get_secret_value()}"},
            )
            if response.status_code != 200:
                raise GarminAPIError(
                    f"API key exchange failed: HTTP {response.status_code}"
                    " (is GARMIN_API_KEY correct and unexpired?)"
                )
            token, expires_at = self._parse_token_response(response)
            self._token = token
            self._token_expires_at = expires_at or self._clock() + DEFAULT_TOKEN_TTL_SECONDS
            log.info(
                "obtained Garmin access token (valid %.0fs)", self._token_expires_at - self._clock()
            )
            return token

    def _parse_token_response(self, response: httpx.Response) -> tuple[str, float | None]:
        """(token, expires_at epoch seconds or None).

        Garmin answers with the bare JWT as text/plain. A JSON body with the usual OAuth
        field names is accepted too, in case that changes.
        """
        if "json" in response.headers.get("content-type", ""):
            try:
                payload = response.json()
            except ValueError as exc:
                raise GarminAPIError("API key exchange returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise GarminAPIError("API key exchange returned an unexpected shape")
            token = _first(payload, "access_token", "accessToken", "token")
            if not isinstance(token, str) or not token:
                # Field names only: the values may be secrets.
                raise GarminAPIError(
                    f"API key exchange response has no token field; got {sorted(payload)}"
                )
            expires_in = _first(payload, "expires_in", "expiresIn")
            if expires_in:
                return token, self._clock() + float(expires_in)
            return token, _jwt_expiry(token)

        token = response.text.strip().strip('"')
        if not TOKEN_PATTERN.fullmatch(token):
            raise GarminAPIError(
                "API key exchange returned an unexpected body"
                f" ({response.headers.get('content-type')}, {len(response.content)} bytes)"
            )
        return token, _jwt_expiry(token)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON endpoint. Returns None for 204/empty responses.

        Re-authenticates once on 401 (e.g. a token revoked early) and retries 429/5xx
        with exponential backoff, honouring Retry-After.
        """
        reauthenticated = False
        attempt = 0
        while True:
            token = await self._access_token(force_new=reauthenticated and attempt == 0)
            response = await self._http.get(
                self._base_url + path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            status = response.status_code

            if status == 401 and not reauthenticated:
                reauthenticated = True
                attempt = 0
                continue
            if (status == 429 or status >= 500) and attempt < MAX_RETRIES:
                delay = self._retry_delay(response, attempt)
                log.warning("GET %s: HTTP %d, retrying in %.0fs", path, status, delay)
                await self._sleep(delay)
                attempt += 1
                continue
            if status == 204 or (status == 200 and not response.content):
                return None
            if status != 200:
                raise GarminAPIError(f"GET {path}: HTTP {status}")
            try:
                return response.json()
            except ValueError as exc:
                raise GarminAPIError(f"GET {path}: response is not JSON") from exc

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return float(retry_after)
        return self._retry_base_delay * 2**attempt
