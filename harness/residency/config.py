"""Config model for the Gable summon adapter (WP-H9).

Config-driven everything: server URL + key path, summon triggers, the
container-launch contract (command + argv), budgets, and persisted-state
paths all live in a plink-owned TOML file OUTSIDE the container (mounted at
/config, read-only). NOTHING in a chat message can reach these fields — the
adapter reads them once at startup and never lets channel text mutate them.

Defaults reference the documented production layout (run-resident.sh under
/usr/local/lib/disjorn, /config, /home/resident) but every value is
overridable, so a scratch/test deployment never touches prod.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "ServerConfig",
    "SummonConfig",
    "BackfillConfig",
    "ContainerConfig",
    "BudgetConfig",
    "CursorConfig",
    "TextConfig",
    "AdapterConfig",
    "load_config",
]


@dataclass
class ServerConfig:
    # Overridable; a scratch server uses http://localhost:PORT. The documented
    # prod URL is intentionally NOT the default so nothing hits prod by omission.
    url: str = "http://localhost:8000"
    # The key is read from a file (plink-owned, mounted ro) — never inlined in
    # a repo. `api_key` inline is a test-only convenience.
    api_key_path: Optional[str] = None
    api_key: Optional[str] = None


@dataclass
class SummonConfig:
    bot_name: str = "gable"
    custodian_channel_id: int = 4
    # The server attaches a `context` block only to a bot's copy of a message
    # that @mentioned (or name-matched) it — that presence IS the mention
    # signal, no client-side name parsing needed.
    trigger_on_context: bool = True
    # Channels where every user message summons (context triggers).
    trigger_channels: list[int] = field(default_factory=list)
    # Extra regex patterns; any search-match on message content summons.
    extra_patterns: list[str] = field(default_factory=list)
    typing_interval_sec: float = 2.5
    # Optional pretty names for #custodian summary legibility.
    channel_names: dict[int, str] = field(default_factory=dict)


@dataclass
class BackfillConfig:
    count: int = 30  # default recent messages pulled to seed the session prompt
    # Per-channel depth overrides. Design threads in #custodian run long, so a
    # deeper window there than the #main default; same sub-table idiom as
    # summon.channel_names.
    per_channel: dict[int, int] = field(default_factory=dict)

    def count_for(self, channel_id: int) -> int:
        """Backfill depth for a channel: the per-channel override if one is
        configured, else the default count."""
        return self.per_channel.get(channel_id, self.count)


@dataclass
class ContainerConfig:
    # The container-launch contract. In prod this is the run-resident.sh
    # wrapper; in tests it is a stub script. argv is entirely config-derived:
    #   [*command, resident, *session_argv, ("--model", model) if pinned]
    # and the assembled prompt is fed on STDIN — never spliced into argv.
    command: list[str] = field(default_factory=list)
    resident: str = "gable"
    session_argv: list[str] = field(default_factory=list)
    # WP-L5 model pin: the model the summoned session MUST run. When set the
    # launcher appends `--model <model>` to the argv (config, never chat), so
    # the session never silently rides the API key's account default. No
    # fallback — a session that can't run the pin fails loud. None = unpinned
    # (documented legacy behaviour; prod always pins).
    model: Optional[str] = None
    timeout_sec: float = 1800.0
    # Extra env for the launch subprocess (e.g. RESIDENT_IMAGE). Config only.
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class BudgetConfig:
    daily_session_cap: int = 12
    # Persisted counter file — survives daemon restarts.
    state_path: str = "/home/resident/.summon-budget.json"


@dataclass
class CursorConfig:
    # Persisted per-channel seq high-water mark — the reconnect-from-seq
    # handoff across daemon restarts.
    state_path: str = "/home/resident/.summon-cursor.json"


@dataclass
class TextConfig:
    refusal_line: str = (
        "I'm at my summon budget for today — flag a human in #custodian "
        "if this can't wait."
    )
    error_line: str = (
        "Something went wrong running that on my end; a human can check "
        "#custodian for the details."
    )


def _parse_model(raw) -> Optional[str]:
    """The [container].model pin, validated fail-loud (WP-L5).

    Absent → None (unpinned). Present → must be a non-empty string; a blank or
    non-string value is config drift and raises at load time, never a silent
    empty pin that would fall through to the account default.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "container.model must be a non-empty string when set "
            f"(got {raw!r})"
        )
    return raw.strip()


@dataclass
class AdapterConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    summon: SummonConfig = field(default_factory=SummonConfig)
    backfill: BackfillConfig = field(default_factory=BackfillConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    cursor: CursorConfig = field(default_factory=CursorConfig)
    text: TextConfig = field(default_factory=TextConfig)

    # ------------------------------------------------------------------ build

    @classmethod
    def from_dict(cls, data: dict) -> "AdapterConfig":
        srv = data.get("server", {}) or {}
        sm = data.get("summon", {}) or {}
        bf = data.get("backfill", {}) or {}
        cn = data.get("container", {}) or {}
        bg = data.get("budget", {}) or {}
        cu = data.get("cursor", {}) or {}
        tx = data.get("text", {}) or {}

        return cls(
            server=ServerConfig(
                url=srv.get("url", ServerConfig.url),
                api_key_path=srv.get("api_key_path"),
                api_key=srv.get("api_key"),
            ),
            summon=SummonConfig(
                bot_name=sm.get("bot_name", SummonConfig.bot_name),
                custodian_channel_id=int(
                    sm.get("custodian_channel_id", SummonConfig.custodian_channel_id)
                ),
                trigger_on_context=bool(
                    sm.get("trigger_on_context", SummonConfig.trigger_on_context)
                ),
                trigger_channels=[int(c) for c in sm.get("trigger_channels", [])],
                extra_patterns=list(sm.get("extra_patterns", [])),
                typing_interval_sec=float(
                    sm.get("typing_interval_sec", SummonConfig.typing_interval_sec)
                ),
                channel_names={
                    int(k): str(v)
                    for k, v in (sm.get("channel_names", {}) or {}).items()
                },
            ),
            backfill=BackfillConfig(
                count=int(bf.get("count", BackfillConfig.count)),
                per_channel={
                    int(k): int(v)
                    for k, v in (bf.get("per_channel", {}) or {}).items()
                },
            ),
            container=ContainerConfig(
                command=[str(a) for a in cn.get("command", [])],
                resident=str(cn.get("resident", ContainerConfig.resident)),
                session_argv=[str(a) for a in cn.get("session_argv", [])],
                model=_parse_model(cn.get("model")),
                timeout_sec=float(cn.get("timeout_sec", ContainerConfig.timeout_sec)),
                env={str(k): str(v) for k, v in (cn.get("env", {}) or {}).items()},
            ),
            budget=BudgetConfig(
                daily_session_cap=int(
                    bg.get("daily_session_cap", BudgetConfig.daily_session_cap)
                ),
                state_path=str(bg.get("state_path", BudgetConfig.state_path)),
            ),
            cursor=CursorConfig(
                state_path=str(cu.get("state_path", CursorConfig.state_path)),
            ),
            text=TextConfig(
                refusal_line=str(tx.get("refusal_line", TextConfig.refusal_line)),
                error_line=str(tx.get("error_line", TextConfig.error_line)),
            ),
        )

    # --------------------------------------------------------------- secrets

    def resolve_api_key(self) -> str:
        """Return the API key: inline value if set, else read the key file.

        Called by the CLI at startup only; the adapter itself takes an
        already-built client and never touches the key.
        """
        if self.server.api_key:
            return self.server.api_key
        if self.server.api_key_path:
            with open(self.server.api_key_path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        raise ValueError(
            "no API key: set [server].api_key_path (or api_key for tests)"
        )


def load_config(path: str) -> AdapterConfig:
    with open(path, "rb") as fh:
        return AdapterConfig.from_dict(tomllib.load(fh))
