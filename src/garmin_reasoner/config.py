"""Application settings, loaded from environment variables (and `.env` in development)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_ADMIN_PASSWORD_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    garmin_client_id: str
    garmin_client_secret: SecretStr
    redirect_uri: str
    # Guards admin routes and the MCP login page, which is reachable from the internet.
    admin_password: SecretStr
    # Public HTTPS origin (the Tailscale Funnel URL), e.g. https://host.tailnet.ts.net.
    # Used as the OAuth issuer and MCP resource URL, so it must match what clients see.
    public_base_url: str
    database_path: Path = Path("data/garmin.db")

    # Verify against the docs in the Garmin developer portal.
    garmin_authorize_url: str = "https://connect.garmin.com/oauth2Confirm"
    garmin_token_url: str = "https://diauth.garmin.com/di-oauth2-service/oauth/token"

    # Only these clients may obtain MCP tokens (claude.ai's custom connector callback).
    mcp_allowed_redirect_uris: list[str] = [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    ]

    # Background jobs: daily gap-check backfill and SQLite backup.
    jobs_enabled: bool = True
    gap_check_days: int = 30
    backup_dir: Path = Path("backups")
    backup_keep: int = 14

    @field_validator("admin_password")
    @classmethod
    def _password_is_long(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_ADMIN_PASSWORD_LENGTH:
            raise ValueError(f"must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must be an https:// URL")
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
