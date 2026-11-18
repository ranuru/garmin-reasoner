"""ASGI middleware for HTTP hardening that applies to every route.

- `SecurityHeaders`: safe defaults on every response (no caching of health data, no
  MIME sniffing, no framing, HTTPS only). A route that sets one of these headers itself,
  like the login page's stricter CSP, keeps its own value.
- `BodySizeLimit`: refuses request bodies over a limit. The public endpoints
  (MCP JSON-RPC, OAuth forms) only ever need a few kilobytes, and nothing upstream caps
  them, so without this one request could make the app buffer arbitrary amounts.
"""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_HEADERS: dict[bytes, bytes] = {
    b"cache-control": b"no-store",
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    # Only ever served over HTTPS (Tailscale Funnel/Serve). No includeSubDomains: the
    # app doesn't own the rest of the domain.
    b"strict-transport-security": b"max-age=31536000",
    b"content-security-policy": b"default-src 'none'; frame-ancestors 'none'",
}

MAX_BODY_BYTES = 1024 * 1024


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self._app(scope, receive, send)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(k, v) for k, v in DEFAULT_HEADERS.items() if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimit:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self._app(scope, receive, send)

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    return await self._reject(scope, receive, send, 400, "bad Content-Length")
                if declared > self._max_bytes:
                    return await self._reject(scope, receive, send, 413, "request too large")

        # A chunked body has no Content-Length, so count what actually arrives too.
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, 413, "request too large")

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
