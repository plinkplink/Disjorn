"""Application configuration via pydantic-settings.

All values overridable via environment variables or a `.env` file in the
working directory. Defaults are sane for local dev.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    DB_PATH: str = "data/disjorn.db"
    DATA_DIR: str = "data"

    # Security
    SECRET_KEY: str = "dev-secret-change-me"  # HMAC signing (media URLs etc.)
    COOKIE_SECURE: bool = False  # set True behind HTTPS in production

    # Web Push (VAPID) — generated via `cli.py gen-vapid` (WP2)
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_CLAIMS_EMAIL: str = "mailto:admin@example.com"

    # Pluggable services
    OLLAMA_URL: str = "http://localhost:11434"
    STT_MODEL: str = "small"

    # Media (WP6)
    MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024  # generous — trusted users
    MEDIA_URL_TTL: int = 3600  # seconds a signed media URL stays valid
    # Pluggable services (WP8)
    OLLAMA_MODEL: str = "llama3.2"
    STT_ENGINE: str = "faster_whisper"      # key into services.stt.ENGINES
    SUMMARIZE_ENGINE: str = "ollama"        # key into services.summarize.ENGINES

    @property
    def db_path(self) -> Path:
        return Path(self.DB_PATH)

    @property
    def data_dir(self) -> Path:
        return Path(self.DATA_DIR)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached Settings — used by tests after mutating env vars."""
    get_settings.cache_clear()
