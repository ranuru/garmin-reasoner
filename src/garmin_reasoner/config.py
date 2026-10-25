"""Application settings, loaded from environment variables (and `.env` in development)."""

from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_ADMIN_PASSWORD_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Garmin Connect Personal API key (Garmin Connect web -> Settings -> Personal API Key).
    garmin_api_key: SecretStr
    # The expiry you picked when creating the key; used only to warn before it lapses.
    garmin_api_key_expires: date | None = None
    garmin_api_base_url: str = "https://personal.connectapi.garmin.com"

    # Guards admin routes and the MCP login page, which is reachable from the internet.
    admin_password: SecretStr
    # Public HTTPS URL (Tailscale Funnel), e.g. https://host.tailnet.ts.net/garmin. A path
    # lets the app share the hostname with other services. Used as the OAuth issuer and
    # MCP resource URL (<this>/mcp), so it must match what clients see.
    public_base_url: str
    database_path: Path = Path("data/garmin.db")

    # Only these clients may obtain MCP tokens (claude.ai's custom connector callback).
    mcp_allowed_redirect_uris: list[str] = [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    ]

    # Background jobs: periodic sync from Garmin and daily SQLite backup.
    jobs_enabled: bool = True
    sync_interval_minutes: int = 60
    # Each sync re-pulls this many days before the last synced day, to pick up data the
    # watch uploads late (and the day in progress).
    sync_lookback_days: int = 3
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
        try:
            url = AnyHttpUrl(value)
        except ValueError as exc:
            raise ValueError(
                "must be a valid URL, e.g. https://host.tailnet.ts.net/garmin (or"
                " https://localhost until Tailscale Funnel is set up)"
            ) from exc
        if url.query or url.fragment:
            raise ValueError("must not have a query string or fragment")
        return value.rstrip("/")

    @property
    def public_path_prefix(self) -> str:
        """Path part of PUBLIC_BASE_URL ("/garmin"), or "" when served at the root."""
        return urlsplit(self.public_base_url).path.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
