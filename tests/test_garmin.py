import logging

import httpx
import pytest
from fakes import ACCESS_TOKEN, API_KEY, NOW, FakeClock, FakeGarminAPI, make_jwt

from garmin_reasoner.garmin import GarminAPIError, GarminClient

pytestmark = pytest.mark.anyio

PATH = "/usersummary-service/v2/dailysummaries"


async def test_exchanges_key_and_sends_bearer(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve(PATH, {"data": []})

    assert await garmin_client.get(PATH, {"startDate": "2026-10-01"}) == {"data": []}

    # FakeGarminAPI asserts the ApiKey and Bearer headers on every request.
    [request] = garmin_api.data_requests()
    assert request.url.params["startDate"] == "2026-10-01"


async def test_token_is_reused_until_near_expiry(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI, clock: FakeClock
) -> None:
    garmin_api.serve(PATH, {"data": []})

    await garmin_client.get(PATH)
    await garmin_client.get(PATH)
    assert garmin_api.tokens_issued == 1

    clock.now += 86400 - 60  # inside the refresh margin
    await garmin_client.get(PATH)
    assert garmin_api.tokens_issued == 2


async def test_reauthenticates_once_on_401(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI
) -> None:
    responses = iter([httpx.Response(401), httpx.Response(200, json={"ok": True})])
    garmin_api.route(PATH, lambda request: next(responses))

    assert await garmin_client.get(PATH) == {"ok": True}
    assert garmin_api.tokens_issued == 2


async def test_persistent_401_raises(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve(PATH, status_code=401)

    with pytest.raises(GarminAPIError, match="HTTP 401"):
        await garmin_client.get(PATH)
    assert len(garmin_api.data_requests()) == 2


async def test_retries_rate_limits_and_server_errors(
    garmin_api: FakeGarminAPI, settings, clock: FakeClock
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = GarminClient(
        httpx.AsyncClient(transport=httpx.MockTransport(garmin_api.handler)),
        settings.garmin_api_base_url,
        settings.garmin_api_key,
        clock,
        retry_base_delay=2,
        sleep=fake_sleep,
    )
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "30"}),
            httpx.Response(503),
            httpx.Response(200, json=[1]),
        ]
    )
    garmin_api.route(PATH, lambda request: next(responses))

    assert await client.get(PATH) == [1]
    assert sleeps == [30, 4]  # Retry-After honoured, then exponential backoff


async def test_gives_up_after_retries(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve(PATH, status_code=500)

    with pytest.raises(GarminAPIError, match="HTTP 500"):
        await garmin_client.get(PATH)
    assert len(garmin_api.data_requests()) == 4


async def test_empty_response_is_none(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.serve(PATH, status_code=204)
    assert await garmin_client.get(PATH) is None


async def test_token_expiry_comes_from_jwt_exp(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI, clock: FakeClock
) -> None:
    short_lived = make_jwt({"exp": NOW + 600})
    garmin_api.token_response = lambda request: httpx.Response(
        200, text=short_lived, headers={"content-type": "text/plain"}
    )
    garmin_api.route(PATH, lambda request: httpx.Response(200, json=[]))

    await garmin_client.get(PATH)
    clock.now += 400  # past exp minus the refresh margin
    await garmin_client.get(PATH)

    assert len([r for r in garmin_api.requests if r.url.path.endswith("/token")]) == 2


@pytest.mark.parametrize(
    "token_json",
    [{"accessToken": ACCESS_TOKEN, "expiresIn": 3600}, {"access_token": ACCESS_TOKEN}],
)
async def test_accepts_json_token_response(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI, token_json: dict[str, object]
) -> None:
    garmin_api.token_response = lambda request: httpx.Response(200, json=token_json)
    garmin_api.serve(PATH, [])

    assert await garmin_client.get(PATH) == []


async def test_unrecognised_json_token_response_names_fields_not_values(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.token_response = lambda request: httpx.Response(200, json={"jwt": "secret-value"})

    with pytest.raises(GarminAPIError) as error:
        await garmin_client.get(PATH)
    assert "['jwt']" in str(error.value)
    assert "secret-value" not in str(error.value)


async def test_html_body_is_not_used_as_token(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI
) -> None:
    garmin_api.token_response = lambda request: httpx.Response(
        200, text="<html>Service unavailable</html>", headers={"content-type": "text/html"}
    )

    with pytest.raises(GarminAPIError, match="unexpected body"):
        await garmin_client.get(PATH)
    assert garmin_api.data_requests() == []


async def test_rejected_key_is_not_leaked(
    garmin_client: GarminClient, garmin_api: FakeGarminAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    garmin_api.token_response = lambda request: httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(GarminAPIError, match="API key exchange failed: HTTP 401") as error:
        await garmin_client.get(PATH)
    assert API_KEY not in str(error.value)
    assert API_KEY not in caplog.text
    assert ACCESS_TOKEN not in caplog.text
