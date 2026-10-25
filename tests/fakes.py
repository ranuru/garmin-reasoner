"""Shared test doubles and constants."""

import json

import httpx

USER_ID = "user-123"
ACCESS_TOKEN = "test-access-token"
ADMIN_PASSWORD = "test-admin-password"
PUBLIC_BASE_URL = "https://example.ts.net"
TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
USER_ID_URL = "https://apis.garmin.com/wellness-api/rest/user/id"
REGISTRATION_URL = "https://apis.garmin.com/wellness-api/rest/user/registration"


class FakeGarminAPI:
    """Stands in for Garmin's servers: serves canned JSON per (method, URL), records requests."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], httpx.Response] = {}
        self.requests: list[httpx.Request] = []

    def serve(
        self, url: str, payload: object = None, status_code: int = 200, method: str = "GET"
    ) -> None:
        content = b"" if payload is None else json.dumps(payload).encode()
        self.responses[(method, url)] = httpx.Response(status_code, content=content)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.get((request.method, str(request.url)), httpx.Response(404))

    def requests_to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == url]
