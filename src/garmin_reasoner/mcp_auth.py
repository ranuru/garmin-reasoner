"""OAuth authorization server for the MCP endpoint.

claude.ai's custom connector is the OAuth client: it registers itself (dynamic client
registration) or uses an ID and secret created with `python -m garmin_reasoner.mcp_clients`,
sends me to `/authorize`, and exchanges the resulting code for tokens.
The MCP SDK implements those protocol endpoints and their checks (PKCE, code expiry,
redirect URI matching, scopes); this module supplies storage and policy:

- Only redirect URIs in `MCP_ALLOWED_REDIRECT_URIS` may register, so nobody can trick
  me into approving a token for their own client.
- Approving a request takes the admin password, on `/mcp-login`. Failed attempts are
  throttled because that page is on the public internet.
- Codes and tokens are stored hashed. Refresh tokens rotate on use.
"""

import base64
import hashlib
import html
import json
import logging
import secrets
import sqlite3
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from garmin_reasoner import db
from garmin_reasoner.garmin import Clock

log = logging.getLogger(__name__)

SCOPE = "read"
LOGIN_PATH = "/mcp-login"
REQUEST_TTL_SECONDS = 600
CODE_TTL_SECONDS = 300
ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class SQLiteOAuthProvider:
    """`OAuthAuthorizationServerProvider` backed by the app's SQLite database."""

    def __init__(
        self,
        database_path: Path,
        public_base_url: str,
        allowed_redirect_uris: list[str],
        clock: Clock = time.time,
    ) -> None:
        self._database_path = database_path
        self._public_base_url = public_base_url
        self._allowed_redirect_uris = set(allowed_redirect_uris)
        self._clock = clock

    def _connect(self) -> closing[sqlite3.Connection]:
        return closing(db.connect(self._database_path))

    def _now(self) -> int:
        return int(self._clock())

    # --- clients ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT info_json FROM mcp_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["info_json"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirect_uris = {str(uri) for uri in client_info.redirect_uris or []}
        if not redirect_uris or not redirect_uris <= self._allowed_redirect_uris:
            log.warning("refused MCP client registration for redirect URIs %s", redirect_uris)
            raise RegistrationError("invalid_redirect_uri", "redirect URI is not allowed")
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO mcp_clients (client_id, info_json, created_at) VALUES (?, ?, ?)",
                (client_info.client_id, client_info.model_dump_json(), self._now()),
            )
        log.info("registered MCP client %r (%s)", client_info.client_name, client_info.client_id)

    # --- authorization ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the login page to approve it."""
        request_id = secrets.token_urlsafe(32)
        with self._connect() as conn, conn:
            conn.execute(
                "DELETE FROM mcp_auth_requests WHERE created_at < ?",
                (self._now() - REQUEST_TTL_SECONDS,),
            )
            conn.execute(
                "INSERT INTO mcp_auth_requests (request_id, client_id, params_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (request_id, client.client_id, params.model_dump_json(), self._now()),
            )
        return f"{self._public_base_url}{LOGIN_PATH}?{urlencode({'request_id': request_id})}"

    def describe_request(self, request_id: str) -> tuple[str, str] | None:
        """(client name, redirect URI) of a pending request, for the login page."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT c.info_json, r.params_json FROM mcp_auth_requests r"
                " JOIN mcp_clients c USING (client_id)"
                " WHERE r.request_id = ? AND r.created_at >= ?",
                (request_id, self._now() - REQUEST_TTL_SECONDS),
            ).fetchone()
        if row is None:
            return None
        client = OAuthClientInformationFull.model_validate_json(row["info_json"])
        params = AuthorizationParams.model_validate_json(row["params_json"])
        return client.client_name or client.client_id, str(params.redirect_uri)

    def approve_request(self, request_id: str) -> str | None:
        """Consume a pending request and return the client redirect URL carrying a code."""
        with self._connect() as conn, conn:
            rows = conn.execute(
                "DELETE FROM mcp_auth_requests WHERE request_id = ? AND created_at >= ?"
                " RETURNING client_id, params_json",
                (request_id, self._now() - REQUEST_TTL_SECONDS),
            ).fetchall()
            if not rows:
                return None
            client_id = rows[0]["client_id"]
            params = AuthorizationParams.model_validate_json(rows[0]["params_json"])

            code = secrets.token_urlsafe(32)
            auth_code = AuthorizationCode(
                code=code,
                scopes=params.scopes or [SCOPE],
                expires_at=self._now() + CODE_TTL_SECONDS,
                client_id=client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
            )
            conn.execute(
                "INSERT INTO mcp_auth_codes (code_hash, client_id, data_json, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    _hash(code),
                    client_id,
                    auth_code.model_dump_json(exclude={"code"}),
                    auth_code.expires_at,
                ),
            )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM mcp_auth_codes WHERE code_hash = ? AND client_id = ?",
                (_hash(authorization_code), client.client_id),
            ).fetchone()
        if row is None:
            return None
        return AuthorizationCode.model_validate(
            {**json.loads(row["data_json"]), "code": authorization_code}
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._connect() as conn, conn:
            deleted = conn.execute(
                "DELETE FROM mcp_auth_codes WHERE code_hash = ?", (_hash(authorization_code.code),)
            ).rowcount
            if not deleted:  # already used, e.g. a concurrent duplicate request
                raise TokenError("invalid_grant", "authorization code already used")
            return self._issue_tokens(conn, client.client_id, authorization_code.scopes)

    # --- tokens ---

    def _issue_tokens(
        self, conn: sqlite3.Connection, client_id: str, scopes: list[str]
    ) -> OAuthToken:
        grant_id = secrets.token_urlsafe(16)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self._now()
        conn.executemany(
            "INSERT INTO mcp_tokens (token_hash, kind, grant_id, client_id, scopes, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    _hash(access),
                    "access",
                    grant_id,
                    client_id,
                    " ".join(scopes),
                    now + ACCESS_TTL_SECONDS,
                ),
                (
                    _hash(refresh),
                    "refresh",
                    grant_id,
                    client_id,
                    " ".join(scopes),
                    now + REFRESH_TTL_SECONDS,
                ),
            ],
        )
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _load_token(self, token: str, kind: str) -> tuple[str, list[str], int] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT client_id, scopes, expires_at FROM mcp_tokens"
                " WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (_hash(token), kind, self._now()),
            ).fetchone()
        return (row["client_id"], row["scopes"].split(), row["expires_at"]) if row else None

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self._load_token(token, "access")
        if found is None:
            return None
        client_id, scopes, expires_at = found
        return AccessToken(token=token, client_id=client_id, scopes=scopes, expires_at=expires_at)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        found = self._load_token(refresh_token, "refresh")
        if found is None or found[0] != client.client_id:
            return None
        client_id, scopes, expires_at = found
        return RefreshToken(
            token=refresh_token, client_id=client_id, scopes=scopes, expires_at=expires_at
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate: the old refresh token and its access token stop working."""
        with self._connect() as conn, conn:
            if not self._delete_grant(conn, refresh_token.token, kind="refresh"):
                raise TokenError("invalid_grant", "refresh token already used")
            return self._issue_tokens(conn, client.client_id, scopes)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._connect() as conn, conn:
            self._delete_grant(conn, token.token)

    @staticmethod
    def _delete_grant(conn: sqlite3.Connection, token: str, kind: str | None = None) -> bool:
        query = "SELECT grant_id FROM mcp_tokens WHERE token_hash = ?"
        args: tuple[str, ...] = (_hash(token),)
        if kind:
            query += " AND kind = ?"
            args += (kind,)
        row = conn.execute(query, args).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM mcp_tokens WHERE grant_id = ?", (row["grant_id"],))
        return True


class PathPrefix:
    """ASGI wrapper: serve the app under PUBLIC_BASE_URL's path, e.g. /garmin.

    Lets the app share a hostname with another service (Home Assistant owns `/` on
    the Funnel host) without the two colliding on OAuth discovery:

    - `/garmin/...` is routed as `/...`.
    - RFC 8414 puts a path-prefixed issuer's metadata at
      `/.well-known/oauth-authorization-server/garmin`, so that maps to the SDK's
      `/.well-known/oauth-authorization-server` route.
    - The RFC 9728 resource metadata (`/.well-known/oauth-protected-resource/garmin/mcp`)
      already includes the prefix, so the SDK serves it as is.

    Unprefixed paths pass through unchanged, so tailnet-only access (admin) still works.
    """

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self._app = app
        self._prefix = prefix.rstrip("/")
        self._issuer_metadata = f"/.well-known/oauth-authorization-server{self._prefix}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._prefix and scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == self._prefix or path.startswith(self._prefix + "/"):
                scope = self._with_path(scope, path[len(self._prefix) :] or "/")
            elif path == self._issuer_metadata:
                scope = self._with_path(scope, "/.well-known/oauth-authorization-server")
        await self._app(scope, receive, send)

    @staticmethod
    def _with_path(scope: Scope, path: str) -> Scope:
        return {**scope, "path": path, "raw_path": path.encode()}


class BasicAuthForPostClients:
    """ASGI wrapper: lets `client_secret_post` clients authenticate with HTTP Basic too.

    Our metadata advertises both methods, and a client given an ID and secret by hand
    (see `mcp_clients.py`) may use either, but the SDK only accepts the one method a
    client was registered with. For such clients, this rewrites an `Authorization: Basic`
    header on `POST /token` into the equivalent `client_id`/`client_secret` form fields.
    Clients registered with `client_secret_basic` are passed through untouched.
    """

    def __init__(self, app: ASGIApp, provider: SQLiteOAuthProvider) -> None:
        self._app = app
        self._provider = provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/token":
            return await self._app(scope, receive, send)
        headers = dict(scope["headers"])
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if not authorization.lower().startswith("basic "):
            return await self._app(scope, receive, send)
        try:
            decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode()
            client_id, client_secret = (unquote(part) for part in decoded.split(":", 1))
        except (ValueError, UnicodeDecodeError):
            return await self._app(scope, receive, send)
        client = await self._provider.get_client(client_id)
        if client is None or client.token_endpoint_auth_method != "client_secret_post":
            return await self._app(scope, receive, send)

        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        form = parse_qs(body.decode(), keep_blank_values=True)
        form.setdefault("client_id", [client_id])
        form.setdefault("client_secret", [client_secret])
        new_body = urlencode(form, doseq=True).encode()

        new_headers = [
            (k, v) for k, v in scope["headers"] if k not in (b"authorization", b"content-length")
        ]
        new_headers.append((b"content-length", str(len(new_body)).encode()))
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": new_body, "more_body": False}

        await self._app({**scope, "headers": new_headers}, replay, send)


class LoginThrottle:
    """Global cap on failed logins: at most `max_failures` per `window_seconds`.

    Global rather than per-IP because Funnel traffic arrives from Tailscale's relays and
    this is a single-user service; the cost is that an attacker can lock me out briefly.
    """

    def __init__(self, max_failures: int = 10, window_seconds: int = 900, clock: Clock = time.time):
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._clock = clock
        self._failures: deque[float] = deque()

    def _prune(self) -> None:
        cutoff = self._clock() - self._window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def blocked(self) -> bool:
        self._prune()
        return len(self._failures) >= self._max_failures

    def record_failure(self) -> None:
        self._failures.append(self._clock())


# --- login page ---

router = APIRouter()

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def _login_page(
    action: str, request_id: str, client_name: str, redirect_uri: str, error: str = ""
) -> str:
    e = html.escape
    error_html = f'<p class="error">{e(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize access</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 28rem; margin: 4rem auto;
         padding: 0 1rem; }}
  input, button {{ font: inherit; padding: .5rem; width: 100%; box-sizing: border-box;
                   margin-top: .5rem; }}
  .error {{ color: #b00020; }}
  code {{ word-break: break-all; }}
</style></head>
<body>
  <h1>Authorize access</h1>
  <p><strong>{e(client_name)}</strong> wants read access to your Garmin data.</p>
  <p>It will be sent to <code>{e(redirect_uri)}</code>.</p>
  {error_html}
  <form method="post" action="{e(action)}">
    <input type="hidden" name="request_id" value="{e(request_id)}">
    <label>Admin password <input type="password" name="password" autofocus required></label>
    <button type="submit">Approve</button>
  </form>
</body></html>"""


def _login_url(request: Request) -> str:
    # Absolute, so the form posts back here even when served under a path prefix
    # (a bare "/mcp-login" would go to whatever owns the site root).
    return request.app.state.settings.public_base_url + LOGIN_PATH


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=_SECURITY_HEADERS)


@router.get(LOGIN_PATH)
def login_form(request: Request, request_id: str = "") -> Response:
    provider: SQLiteOAuthProvider = request.app.state.mcp_provider
    described = provider.describe_request(request_id)
    if described is None:
        return _html("<p>This authorization request is unknown or has expired.</p>", 400)
    return _html(_login_page(_login_url(request), request_id, *described))


@router.post(LOGIN_PATH)
async def login_submit(request: Request) -> Response:
    provider: SQLiteOAuthProvider = request.app.state.mcp_provider
    throttle: LoginThrottle = request.app.state.login_throttle

    # Parsed by hand to avoid a python-multipart dependency for one urlencoded form.
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    request_id, password = form.get("request_id", ""), form.get("password", "")

    described = provider.describe_request(request_id)
    if described is None:
        return _html("<p>This authorization request is unknown or has expired.</p>", 400)
    if throttle.blocked():
        log.warning("MCP login throttled")
        return _html(
            _login_page(
                _login_url(request), request_id, *described, error="Too many attempts; try later."
            ),
            429,
        )

    expected = request.app.state.settings.admin_password.get_secret_value()
    if not secrets.compare_digest(password.encode(), expected.encode()):
        throttle.record_failure()
        log.warning("failed MCP login attempt")
        return _html(
            _login_page(_login_url(request), request_id, *described, error="Wrong password."), 401
        )

    redirect = provider.approve_request(request_id)
    if redirect is None:
        return _html("<p>This authorization request is unknown or has expired.</p>", 400)
    log.info("approved MCP authorization for %r", described[0])
    return RedirectResponse(redirect, status_code=303, headers=_SECURITY_HEADERS)
