"""CLI entrypoint: python -m consolidation --resident <name> [--dry-run]."""

import io

from consolidation.__main__ import main
from consolidation_testlib import add_memory, append_log, write_spine_entry


def _write_toml(config_dir, resident, *, active, store, spine_dir, log_path):
    (config_dir / f"{resident}.toml").write_text(
        "\n".join([
            f'resident = "{resident}"',
            f"active = {'true' if active else 'false'}",
            "[episodic]",
            f'data_dir = "{store.data_dir}"',
            f'collection = "{store.collection_name}"',
            "[retrieval_log]",
            f'path = "{log_path}"',
            "[spine]",
            f'dir = "{spine_dir}"',
            "[consolidation]",
            "min_spine_age_days = 0",
            "[broker]",
            'cli = "definitely-not-a-real-broker"',
        ]),
        encoding="utf-8",
    )


def _seed(store, spine_dir, log_path):
    add_memory(store, "hot promotable pattern", mid="m-hot")
    for _ in range(4):
        append_log(log_path, returned_ids=["m-hot"], days_ago=1)
    write_spine_entry(spine_dir, "20-plain.md", "Unreferenced plain fact.", name="plain")


def test_dry_run_prints_report_exit_zero(tmp_path, store, spine_dir, log_path, monkeypatch):
    _seed(store, spine_dir, log_path)
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    _write_toml(cdir, "claudette", active=True, store=store, spine_dir=spine_dir, log_path=log_path)
    monkeypatch.setenv("CONSOLIDATION_CONFIG_DIR", str(cdir))

    buf = io.StringIO()
    rc = main(["--resident", "claudette", "--dry-run", "--now", "2026-07-20T12:00:00+00:00"], out=buf)
    assert rc == 0
    assert "PROPOSE PROMOTE" in buf.getvalue()
    assert "PROPOSE EVICT" in buf.getvalue()


def test_inactive_resident_forced_to_dry_run(tmp_path, store, spine_dir, log_path, monkeypatch, capsys):
    _seed(store, spine_dir, log_path)
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    _write_toml(cdir, "gable", active=False, store=store, spine_dir=spine_dir, log_path=log_path)
    monkeypatch.setenv("CONSOLIDATION_CONFIG_DIR", str(cdir))

    buf = io.StringIO()
    # no --dry-run, but resident inactive -> forced dry-run, nothing posted
    rc = main(["--resident", "gable", "--now", "2026-07-20T12:00:00+00:00"], out=buf)
    assert rc == 0
    err = capsys.readouterr().err
    assert "not marked active" in err
    assert "forcing --dry-run" in err
    # it still produced the report to the out buffer (no broker call happened)
    assert "consolidation run for gable" in buf.getvalue()


def test_missing_config_exit_three(tmp_path, monkeypatch):
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    monkeypatch.setenv("CONSOLIDATION_CONFIG_DIR", str(cdir))
    rc = main(["--resident", "ghost", "--dry-run"], out=io.StringIO())
    assert rc == 3


def test_bad_now_exit_two(tmp_path, store, spine_dir, log_path, monkeypatch):
    _seed(store, spine_dir, log_path)
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    _write_toml(cdir, "claudette", active=True, store=store, spine_dir=spine_dir, log_path=log_path)
    monkeypatch.setenv("CONSOLIDATION_CONFIG_DIR", str(cdir))
    rc = main(["--resident", "claudette", "--dry-run", "--now", "not-a-date"], out=io.StringIO())
    assert rc == 2
