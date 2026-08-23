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
    # Picker assets are served raw (no display/thumb conversion) and the popover
    # loads a whole tab at once, so this cap is deliberately much tighter than
    # MAX_UPLOAD_BYTES — one fat GIF degrades the grid for everyone.
    MAX_PICKER_BYTES: int = 8 * 1024 * 1024
    # Pluggable services (WP8)
    OLLAMA_MODEL: str = "llama3.2"
    STT_ENGINE: str = "faster_whisper"      # key into services.stt.ENGINES
    SUMMARIZE_ENGINE: str = "ollama"        # key into services.summarize.ENGINES

    # Plan Room (SPECS/2026-08-20-plan-room.md). The derived card index, written
    # BROKER-SIDE by harness/planroom/planroom.py and opened READ-ONLY here.
    # Derivation needs gatehouse access (`sudo git --git-dir`) and brokerd
    # imports; this process has neither and must not grow them (seq 1428 P2).
    # Empty means the tab reports itself unavailable, which is the honest
    # answer — an absent index and an empty board must not read alike.
    PLANROOM_INDEX: str = ""

    @property
    def db_path(self) -> Path:
        return Path(self.DB_PATH)

    @property
    def planroom_index(self) -> Path | None:
        return Path(self.PLANROOM_INDEX) if self.PLANROOM_INDEX else None

    @property
    def data_dir(self) -> Path:
        return Path(self.DATA_DIR)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached Settings — used by tests after mutating env vars."""
    get_settings.cache_clear()
