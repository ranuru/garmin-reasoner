from fastapi.testclient import TestClient

from garmin_reasoner.config import Settings


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_creates_database(client: TestClient, settings: Settings) -> None:
    assert settings.database_path.exists()


def test_secret_not_exposed_in_repr(settings: Settings) -> None:
    assert "test-client-secret" not in repr(settings)
