import pytest
from fakes import ADMIN_PASSWORD, API_KEY
from fastapi.testclient import TestClient
from pydantic import ValidationError

from garmin_reasoner.config import Settings


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_creates_database(client: TestClient, settings: Settings) -> None:
    assert settings.database_path.exists()


def test_secrets_not_exposed_in_repr(settings: Settings) -> None:
    assert API_KEY not in repr(settings)
    assert ADMIN_PASSWORD not in repr(settings)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "garmin_api_key": API_KEY,
        "admin_password": ADMIN_PASSWORD,
        "public_base_url": "https://localhost",
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.mark.parametrize(
    "url, problem",
    [
        ("https://<your-host>.<your-tailnet>.ts.net", "must be a valid URL"),
        ("http://host.tailnet.ts.net", "must be an https:// URL"),
    ],
)
def test_public_base_url_is_validated_up_front(url: str, problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        make_settings(public_base_url=url)


def test_public_base_url_trailing_slash_is_dropped() -> None:
    assert (
        make_settings(public_base_url="https://h.t.ts.net/").public_base_url == "https://h.t.ts.net"
    )


def test_short_admin_password_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 16 characters"):
        make_settings(admin_password="...")
