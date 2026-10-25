"""Shared test doubles and constants."""

import json

import httpx

USER_ID = "user-123"
ACCESS_TOKEN = "test-access-token"


class FakeGarminAPI:
    """Stands in for apis.garmin.com: serves canned JSON per URL and records requests."""

    def __init__(self) -> None:
        self.responses: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []

    def serve(self, url: str, payload: object, status_code: int = 200) -> None:
        self.responses[url] = httpx.Response(status_code, content=json.dumps(payload))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.get(str(request.url), httpx.Response(404))
