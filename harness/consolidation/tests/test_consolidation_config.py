"""Config loading: shipped Claudette (active) + Gable (inactive) configs."""

import pytest

from consolidation.config import (
    PACKAGED_CONFIG_DIR,
    BROKER_CLI_ENV,
    load_config,
)


def test_claudette_ships_active():
    cfg = load_config("claudette", PACKAGED_CONFIG_DIR)
    assert cfg.resident == "claudette"
    assert cfg.active is True  # first to activate
    assert cfg.episodic_collection == "claudette_memory"
    assert cfg.spine_dir.endswith("spine")
    assert cfg.window_days > 0
    assert "lesson" in cfg.constraint_tags


def test_gable_ships_inactive():
    cfg = load_config("gable", PACKAGED_CONFIG_DIR)
    assert cfg.resident == "gable"
    assert cfg.active is False  # second client, not yet switched on
    assert cfg.episodic_collection == "gable_memory"


def test_missing_config_raises():
    with pytest.raises(FileNotFoundError):
        load_config("nobody", PACKAGED_CONFIG_DIR)


def test_broker_cli_env_override(monkeypatch):
    monkeypatch.setenv(BROKER_CLI_ENV, "/custom/broker")
    cfg = load_config("claudette", PACKAGED_CONFIG_DIR)
    assert cfg.broker_cli == "/custom/broker"


def test_config_dir_env(monkeypatch, tmp_path):
    (tmp_path / "zed.toml").write_text(
        '\n'.join([
            'resident = "zed"',
            'active = true',
            '[episodic]',
            'data_dir = "/x/chroma"',
            'collection = "zed_memory"',
            '[retrieval_log]',
            'path = "/x/log.jsonl"',
            '[spine]',
            'dir = "/x/spine"',
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSOLIDATION_CONFIG_DIR", str(tmp_path))
    cfg = load_config("zed")  # no explicit dir -> env wins
    assert cfg.episodic_collection == "zed_memory"
    # unspecified knobs fall back to dataclass defaults
    assert cfg.window_days == 30
