"""Application settings, loaded from environment variables (and `.env` in development)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    garmin_client_id: str
    garmin_client_secret: SecretStr
    redirect_uri: str
    admin_password: SecretStr
    database_path: Path = Path("data/garmin.db")

    # Verify against the docs in the Garmin developer portal.
    garmin_authorize_url: str = "https://connect.garmin.com/oauth2Confirm"
    garmin_token_url: str = "https://diauth.garmin.com/di-oauth2-service/oauth/token"


@lru_cache
def get_settings() -> Settings:
    return Settings()
