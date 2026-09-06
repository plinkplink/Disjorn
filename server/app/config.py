"""Application configuration via pydantic-settings.

All values overridable via environment variables or a `.env` file in the
working directory. Defaults are sane for local dev.
"""

import re
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
    SECRET_KEY: str = "dev-secret-change-me"  # HMAC signing (media URLs etc.)
    # The session cookie carries the __Host- prefix, which a browser honours
    # only on a Secure cookie, so False drops every login silently. main.py
    # refuses to boot on it.
    COOKIE_SECURE: bool = False

    # Origins allowed to make a cookie-authenticated write or open a
    # cookie-authenticated /ws handshake (app/origin_wall.py). Exact string
    # match, never a suffix test: the apps port :8443 is a different origin and
    # must stay out of this list. Dev origins go in the list, never into the
    # code. Empty is a house-wide write lockout, so main.py refuses to boot on
    # it. Set from the environment as a JSON array:
    #   HOUSE_ORIGINS=["https://debian.tailca81ba.ts.net"]
    HOUSE_ORIGINS: list[str] = []

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

    # APPS tab (SPECS/2026-08-30-apps-tab-v1.md, stage 1).
    #
    # These four live HERE rather than in broker.toml's `[apps]` table because
    # this process never reads broker.toml and must not learn how (the
    # PLANROOM_INDEX precedent above). The split is by ENFORCER, not by topic:
    # what the SERVER enforces — the daily session cap, the lock lifetime, who
    # may build, who may publish a stage — is server config; what the BROKER
    # enforces — the per-build token ceiling, the serving origin base, the
    # apps-builder spawn — stays in broker.toml for a later stage. A knob whose
    # enforcer cannot read it is not a knob.

    # Build sessions per user per UTC day (RULED seq 2176: 3). The visible
    # unit; there is deliberately no daily token cap.
    APPS_DAILY_SESSION_CAP: int = 3

    # Seconds a build session's lock survives without a heartbeat (RULED at
    # Round 12: 15 minutes). The open modal heartbeats; a lock whose
    # locked_until has passed is clear, and no sweeper is involved.
    APPS_SESSION_LOCK_TTL: int = 900

    # The resident seats offered as builders in the chooser, in the order they
    # should be shown. JSON array of objects:
    #   APPS_BUILDERS=[{"bot_id": 2, "model_source": "/etc/gable/seat.toml"}]
    # `model_source` is a path READ AT REQUEST TIME to print the model the seat
    # actually runs (see read_model_pin). Empty is allowed and is not a boot
    # failure — the tab renders an empty state that says so, which is the
    # honest answer for a house that has not configured a build seat yet.
    APPS_BUILDERS: list[dict[str, Any]] = []

    # Bot names allowed to POST a build-stage event (plus admin users). The
    # stage stream is an attestation about what a build actually did, so its
    # publisher list is config a person edits, never something a caller claims.
    APPS_STAGE_PUBLISHER_BOT_NAMES: list[str] = ["broker"]

    @property
    def db_path(self) -> Path:
        return Path(self.DB_PATH)

    @property
    def planroom_index(self) -> Path | None:
        return Path(self.PLANROOM_INDEX) if self.PLANROOM_INDEX else None

    @property
    def data_dir(self) -> Path:
        return Path(self.DATA_DIR)


# ---------------------------------------------------------------------------
# Model pin: printed from the seat, never written down here
# ---------------------------------------------------------------------------
#
# Provenance-printing policy (spec "Agreed UX"): a builder card shows the model
# the seat ACTUALLY RUNS, read from the seat's own config at request time.
# There is no model string anywhere in this codebase and there must never be
# one — a hardcoded name is a card that keeps saying the old model for months
# after the seat was repinned, and nobody can tell it is lying.
#
# Unreadable, absent, or silent about its model -> None, and the card says
# "model not declared by seat". That is a true statement about the seat; a
# guessed name would not be.

# `model = "..."` at the TOP LEVEL of a .toml — before any [table] header. A
# `model` key inside `[some.other.table]` belongs to that table and is not the
# seat's pin.
_TOML_MODEL_RE = re.compile(r"""^\s*model\s*=\s*(["'])(?P<value>[^"']*)\1\s*(?:#.*)?$""")
_TOML_TABLE_RE = re.compile(r"^\s*\[")

# env-style: CHAT_MODEL=… wins over MODEL=… when a file carries both, because
# a seat that names both means the specific one.
_ENV_MODEL_RE = re.compile(
    r"""^\s*(?:export\s+)?(?P<key>CHAT_MODEL|MODEL)\s*=\s*(?P<value>.*?)\s*$"""
)

# Bounds. A "model source" is a small config file; anything past these is not
# one, and reading it into a response would make an API shape out of whatever
# is on disk at that path.
MODEL_PIN_MAX_BYTES = 256 * 1024
MODEL_PIN_MAX_CHARS = 200


def _clean_env_value(raw: str) -> str:
    """Strip an inline comment and one layer of matching quotes."""
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:end] if end > 0 else raw[1:]
    return raw.split("#", 1)[0].strip()


def read_model_pin(source: str | Path | None) -> str | None:
    """The model a builder seat declares, or None if it declares none.

    `source` is a path from APPS_BUILDERS[*]["model_source"]. A `.toml` suffix
    is read as TOML (top-level `model = "..."`); anything else is read as an
    env-style file (`CHAT_MODEL=` / `MODEL=`). Every failure mode — no path, no
    file, no permission, no model line, a value longer than a model name could
    plausibly be — answers None rather than raising or guessing, because the
    builder chooser must still render when a seat's config is missing.
    """
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
        for line in text.splitlines():
            if _TOML_TABLE_RE.match(line):
                break  # past the top level; a later `model =` is not the pin
            match = _TOML_MODEL_RE.match(line)
            if match:
                value = match.group("value").strip()
                return value[:MODEL_PIN_MAX_CHARS] or None
        return None

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
    """Clear the cached Settings — used by tests after mutating env vars."""
    get_settings.cache_clear()
