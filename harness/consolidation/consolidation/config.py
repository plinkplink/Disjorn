"""Per-resident consolidation config (TOML), matching the house's config style.

Claudette is the first client, Gable the second. Both ship; Claudette's is
marked `active = true` (first to activate), Gable's `active = false`. A
non-active resident is refused real posting — it can only ever run `--dry-run`
until plink flips the switch. Nothing here is reachable from inside a
container: like the broker's config, activation is a lever held outside.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Packaged config dir (harness/consolidation/config), overridable via env so
# tests point at synthetic configs and prod points at the mounted lever.
PACKAGED_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CONFIG_DIR_ENV = "CONSOLIDATION_CONFIG_DIR"
BROKER_CLI_ENV = "CONSOLIDATION_BROKER_CLI"

DEFAULT_BROKER_CLI = "/usr/local/bin/broker"


@dataclass
class ConsolidationConfig:
    resident: str
    active: bool

    # episodic store (house_memory.MemoryStore)
    episodic_data_dir: str
    episodic_collection: str

    # unified retrieval log (house_memory.RetrievalLog)
    retrieval_log_path: str

    # markdown spine (house_memory.Spine). OPTIONAL: `None` means "this
    # resident has no on-disk spine" — a real deployment shape, not an error.
    # Claudette's spine is her system prompt (managed through her bot config),
    # not a directory of markdown entries, so her config declares no dir and
    # her run does the episodic-promotion half only. A dir that IS configured
    # but does not exist is a hard error (see analyze._open_spine) — a stale
    # path must never be read as "empty spine, evict everything".
    spine_dir: Optional[str]

    # consolidation knobs
    soft_target_spine_size: int = 60
    window_days: int = 30
    promote_min_references: int = 3
    evict_max_references: int = 0
    min_spine_age_days: int = 30
    exclude_kernel: bool = True
    max_promotions: int = 10
    constraint_tags: list[str] = field(
        default_factory=lambda: [
            "lesson",
            "why",
            "promise",
            "constraint",
            "boundary",
            "rule",
        ]
    )
    constraint_keywords: list[str] = field(
        default_factory=lambda: [
            "never",
            "always",
            "because",
            "must not",
            "do not",
            "learned",
            "the reason",
        ]
    )

    # how proposals reach #custodian (the broker file-proposal verb CLI)
    broker_cli: str = DEFAULT_BROKER_CLI


def _optional_dir(value) -> Optional[str]:
    """`[spine] dir` may be omitted, null, or "" — all meaning "this resident
    has no on-disk spine". Anything else is taken literally (and must exist at
    run time; a configured-but-missing dir is a hard error, never an empty
    spine)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def config_dir(explicit: str | os.PathLike | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env)
    return PACKAGED_CONFIG_DIR


def load_config(
    resident: str, config_dir_path: str | os.PathLike | None = None
) -> ConsolidationConfig:
    """Load `<config_dir>/<resident>.toml`. Env CONSOLIDATION_BROKER_CLI, if
    set, overrides the broker CLI path (test hook / deployment override)."""
    cdir = config_dir(config_dir_path)
    path = cdir / f"{resident}.toml"
    if not path.exists():
        raise FileNotFoundError(f"no consolidation config for resident {resident!r}: {path}")
    with open(path, "rb") as f:
        data = tomllib.load(f)

    episodic = data.get("episodic", {})
    rlog = data.get("retrieval_log", {})
    spine = data.get("spine", {})
    cons = data.get("consolidation", {})
    broker = data.get("broker", {})

    broker_cli = os.environ.get(BROKER_CLI_ENV) or broker.get("cli", DEFAULT_BROKER_CLI)

    kwargs: dict = {
        "resident": data.get("resident", resident),
        "active": bool(data.get("active", False)),
        "episodic_data_dir": episodic["data_dir"],
        "episodic_collection": episodic["collection"],
        "retrieval_log_path": rlog["path"],
        "spine_dir": _optional_dir(spine.get("dir")),
        "broker_cli": broker_cli,
    }
    # optional knobs (fall back to dataclass defaults when absent)
    for key in (
        "soft_target_spine_size",
        "window_days",
        "promote_min_references",
        "evict_max_references",
        "min_spine_age_days",
        "exclude_kernel",
        "max_promotions",
        "constraint_tags",
        "constraint_keywords",
    ):
        if key in cons:
            kwargs[key] = cons[key]

    return ConsolidationConfig(**kwargs)
