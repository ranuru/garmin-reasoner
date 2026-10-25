"""Shared test doubles and constants."""

import base64
import json
from collections.abc import Callable

import httpx

API_KEY = "test-api-key"
ADMIN_PASSWORD = "test-admin-password"
PUBLIC_BASE_URL = "https://example.ts.net"
GARMIN_BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
NOW = 1_800_000_000  # 2027-01-15T08:00:00Z


def make_jwt(claims: dict[str, object]) -> str:
    """An unsigned JWT-shaped token, like the one Garmin's key exchange returns."""

    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


ACCESS_TOKEN = make_jwt({"exp": NOW + 86400, "iat": NOW, "scope": ["read"]})

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeGarminAPI:
    """Stands in for the Personal API: issues tokens, serves canned JSON per path.

    Requests to data endpoints must carry the current bearer token, so tests notice if
    the client forgets it.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.token_response: Handler = self._issue_token
        # Answers paths with no explicit route (404 if unset).
        self.default: Handler | None = None
        self.issued_token: str | None = None

    def _issue_token(self, request: httpx.Request) -> httpx.Response:
        self.tokens_issued += 1
        # Garmin's real behaviour: the bare JWT as text/plain.
        return httpx.Response(200, text=ACCESS_TOKEN, headers={"content-type": "text/plain"})

    @staticmethod
    def _token_in(response: httpx.Response) -> str | None:
        if "json" in response.headers.get("content-type", ""):
            body = response.json()
            return next(
                (body[k] for k in ("access_token", "accessToken", "token") if k in body), None
            )
        return response.text

    def serve(self, path: str, payload: object = None, status_code: int = 200) -> None:
        content = b"" if payload is None else json.dumps(payload).encode()
        self.routes[path] = lambda request: httpx.Response(status_code, content=content)

    def route(self, path: str, handler: Handler) -> None:
        self.routes[path] = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            assert request.method == "POST"
            assert request.headers["Authorization"] == f"ApiKey {API_KEY}"
            response = self.token_response(request)
            self.issued_token = self._token_in(response)
            return response
        assert request.headers["Authorization"] == f"Bearer {self.issued_token}"
        handler = self.routes.get(request.url.path, self.default)
        return handler(request) if handler else httpx.Response(404)

    def data_requests(self, path: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.path != TOKEN_PATH and (path is None or r.url.path == path)
        ]
