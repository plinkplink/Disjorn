"""Settings from the environment or a `.env` in the working directory."""

import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    DB_PATH: str = "data/disjorn.db"
    DATA_DIR: str = "data"

    # Security
    SECRET_KEY: str = "dev-secret-change-me"  # signs media URLs; dev default only
    # False drops every login: the __Host- cookie needs Secure. main.py refuses to boot on it.
    COOKIE_SECURE: bool = False
    # Exact-match origins for cookie writes and /ws. Empty locks out all writes.
    HOUSE_ORIGINS: list[str] = []

    # VAPID keys: `cli.py gen-vapid`
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_CLAIMS_EMAIL: str = "mailto:admin@example.com"

    # Pluggable services
    OLLAMA_URL: str = "http://localhost:11434"
    STT_MODEL: str = ""

    # Media
    MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024
    MEDIA_URL_TTL: int = 3600  # seconds
    BOT_READ_BYTES: int = 1024 * 1024
    # Keep well under MAX_UPLOAD_BYTES: picker assets load together.
    MAX_PICKER_BYTES: int = 8 * 1024 * 1024
    OLLAMA_MODEL: str = "llama3.2"
    STT_ENGINE: str = "faster_whisper"      # key into services.stt.ENGINES
    STT_MODEL_DIR: str = ""
    STT_QUANTIZATION: str = ""
    STT_LANGUAGE: str = "en"
    STT_PRELOAD: bool = False
    STT_MAX_BYTES: int = 32 * 1024 * 1024
    STT_REMOTE_URL: str = ""
    STT_REMOTE_MODEL: str = "large-v3"
    STT_REMOTE_TOKEN: str = ""
    STT_REMOTE_TIMEOUT: float = 20.0
    STT_REMOTE_CONNECT_TIMEOUT: float = 0.5
    STT_REMOTE_COOLDOWN: float = 60.0
    SUMMARIZE_ENGINE: str = "ollama"        # key into services.summarize.ENGINES
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-haiku-5-5"
    SUMMARIZE_CACHE_TTL: int = 7 * 24 * 3600  # seconds
    SUMMARIZE_RATE_LIMIT: int = 20          # uncached summaries per user per window; 0 disables
    SUMMARIZE_RATE_WINDOW: int = 3600       # seconds
    FETCH_CONTACT: str = ""                 # URL or role address sent in page fetchers' User-Agent

    # Broker writes it; read-only here, no gatehouse access. Empty: tab unavailable.
    PLANROOM_INDEX: str = ""

    APPROVAL_ENABLED: bool = False
    APPROVAL_PRINCIPALS: str = "plink,res-claudette,res-gable"
    APPROVAL_RELAY_BOT_NAMES: list[str] = ["broker"]
    BACKLOG_RELAY_BOT_NAMES: list[str] = ["broker"]
    FILES_RELAY_BOT_NAMES: list[str] = ["broker"]
    BOT_DAILY_UPLOAD_BYTES: int = 25 * 1024 * 1024
    BOT_FILES_EXPORT_ROOT: str = "/srv/disjorn-files-ro"
    BOT_FILES_EXPORT_SEATS: list[str] = ["res-gable"]
    BOT_FILES_SWEEP_SECONDS: float = 60  # 0 runs no timer
    BOT_FILES_EXPORT_MAX_BYTES: int = 2 * 1024 ** 3  # per seat; the newest files that fit

    CUSTODIAN_CHANNEL_ID: int = 4

    # Apps settings: this process never reads broker.toml.

    # Per user per UTC day. No token cap is deliberate.
    APPS_DAILY_SESSION_CAP: int = 3

    # Seconds. No sweeper: a lock past its expiry is clear.
    APPS_SESSION_LOCK_TTL: int = 900

    # Builder seats in display order: JSON objects with bot_id, model_source, model_key.
    APPS_BUILDERS: list[dict[str, Any]] = []

    # Bots, plus admins, that may post build-stage events.
    APPS_STAGE_PUBLISHER_BOT_NAMES: list[str] = ["broker"]

    # Gate settings. Not needed to boot; origin endpoints answer 503 without them.

    # Shared with the gate; at least 32 bytes.
    APPS_GATE_SECRET: str = ""

    # Scheme, host and port, no trailing slash. Never add it to HOUSE_ORIGINS.
    APPS_ORIGIN_BASE: str = ""

    # Published app trees, read-only here.
    APPS_WWW_ROOT: str = "/srv/apps-www"

    # Argv prefix, spawned directly and never through a shell.
    APPS_LAUNCH_HELPER: list[str] = [
        "sudo", "-n", "/usr/local/lib/disjorn/disjorn-apps-launch",
    ]

    BROKER_SOCKET_PATH: str = "/run/disjorn-broker/broker.sock"

    PLATFORM_BUILD_BOT: str = "BuildGable"

    MENTION_ONLY_CHANNEL_IDS: list[int] = []
    MENTION_ONLY_CHANNEL_NAMES: list[str] = ["custodian"]

    GROUP_MENTION_SKIP_BOT_NAMES: list[str] = ["system", "broker", "BuildGable"]

    @property
    def db_path(self) -> Path:
        return Path(self.DB_PATH)

    @property
    def approval_principals(self) -> list[str]:
        return [p.strip() for p in self.APPROVAL_PRINCIPALS.split(",") if p.strip()]

    @property
    def planroom_index(self) -> Path | None:
        return Path(self.PLANROOM_INDEX) if self.PLANROOM_INDEX else None

    @property
    def data_dir(self) -> Path:
        return Path(self.DATA_DIR)


# Model pin
#
# Never hardcode a model name: a stale one outlives a repin.

DEFAULT_MODEL_KEY = "model"

_ENV_MODEL_RE = re.compile(
    r"""^\s*(?:export\s+)?(?P<key>CHAT_MODEL|MODEL)\s*=\s*(?P<value>.*?)\s*$"""
)

# Model sources are small config files; larger ones are rejected.
MODEL_PIN_MAX_BYTES = 256 * 1024
MODEL_PIN_MAX_CHARS = 200


def _clean_env_value(raw: str) -> str:
    """Strip an inline comment and one layer of matching quotes."""
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:end] if end > 0 else raw[1:]
    return raw.split("#", 1)[0].strip()


def read_model_pin(source: str | Path | None, model_key: str | None = None) -> str | None:
    """The model a builder seat declares; None on any failure, so the chooser still renders."""
    if not source:
        return None
    path = Path(source)
    try:
        if path.stat().st_size > MODEL_PIN_MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if path.suffix == ".toml":
        try:
            node: Any = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        for part in (model_key or DEFAULT_MODEL_KEY).split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        if not isinstance(node, str):
            return None
        return node.strip()[:MODEL_PIN_MAX_CHARS] or None

    fallback: str | None = None
    for line in text.splitlines():
        match = _ENV_MODEL_RE.match(line)
        if match is None:
            continue
        value = _clean_env_value(match.group("value"))[:MODEL_PIN_MAX_CHARS]
        if not value:
            continue
        if match.group("key") == "CHAT_MODEL":
            return value
        if fallback is None:
            fallback = value
    return fallback


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached Settings, for tests that change env vars."""
    get_settings.cache_clear()
