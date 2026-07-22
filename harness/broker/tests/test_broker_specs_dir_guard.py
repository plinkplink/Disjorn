"""BL-D1 — the start-build confirm gate's REAL authorization surface.

The confirm record ("Confirmed by" + "#custodian seq") is a presence check on
text. It only means anything because the text lives in a directory residents
cannot write. That invariant used to live in a comment; here it is a
construction-time assertion with adversarial tests.

Every test below is a way a resident could come to own the bytes the gate
reads: specs_dir AT a resident home, a symlink INTO one, a resident-writable
PARENT, a group a resident is in, world-writable, or exposed through a
path_map. Each must refuse LOUDLY (ConfigError naming the path) at broker
construction — never at request time, never silently.

Perms are exercised with real stat() on real directories. Resident IDENTITY is
injected (`gids_for_uid`, `broker_uid`) so the cases do not need real users:
the broker's own euid is deliberately not treated as a resident (a caller
running as the broker is the broker), which is also what lets the socket
harness map the running uid to res-test.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest

from brokerd import Broker, ConfigError, assert_specs_dir_resident_unwritable, main

ME = os.getuid()
OTHER = 424242          # a uid that is not us and has no passwd entry
UIDS = {OTHER: "res-gable"}


def _check(specs_dir, *, uid_map=None, residents=None, broker_uid=ME, gids=None):
    return assert_specs_dir_resident_unwritable(
        str(specs_dir),
        uid_map=UIDS if uid_map is None else uid_map,
        residents={} if residents is None else residents,
        broker_uid=broker_uid,
        gids_for_uid=(lambda uid: set()) if gids is None else gids,
    )


# ------------------------------------------------------------- happy path

def test_plink_owned_specs_dir_is_accepted(tmp_path):
    specs = tmp_path / "SPECS"
    specs.mkdir(mode=0o755)
    assert _check(specs) == os.path.realpath(specs)


def test_returns_realpath_so_the_checked_path_is_the_used_path(tmp_path):
    """The guard returns the resolved path; the broker reads SPECS/ through it
    (_specs_dir), so the verified directory and the used directory are the
    same string even if config is mutated later."""
    real = tmp_path / "real-specs"
    real.mkdir(mode=0o755)
    link = tmp_path / "link-specs"
    link.symlink_to(real)
    assert _check(link) == str(real.resolve())


# ------------------------------------------------- RULE 1: resident volumes

def test_specs_dir_inside_a_resident_home_is_refused():
    """The headline case: repoint specs_dir at a resident's own home and every
    confirm record becomes self-attestation."""
    with pytest.raises(ConfigError) as ei:
        _check("/home/res-gable/resident-home/SPECS")
    msg = str(ei.value)
    assert "/home/res-gable" in msg and "resident-writable" in msg


def test_resident_home_itself_is_refused():
    with pytest.raises(ConfigError):
        _check("/home/res-gable")


def test_sibling_directory_is_not_confused_for_a_resident_home(tmp_path):
    """/home/res-gable-evil must not match /home/res-gable by prefix — but it
    must also not be silently accepted: it does not exist, so RULE 2 refuses."""
    with pytest.raises(ConfigError) as ei:
        _check("/home/res-gable-evil/SPECS")
    assert "does not exist" in str(ei.value)


def test_symlink_into_a_resident_home_is_refused(tmp_path):
    """A specs_dir that LOOKS safe but resolves into a resident volume. The
    guard realpath()s before deciding, so the link is followed."""
    link = tmp_path / "SPECS"
    link.symlink_to("/home/res-gable/resident-home/SPECS")
    with pytest.raises(ConfigError) as ei:
        _check(link)
    assert "/home/res-gable" in str(ei.value)


def test_specs_dir_under_a_path_map_target_inside_a_resident_home(tmp_path):
    """A path_map host target that lives in a resident volume is a resident
    volume, and specs_dir under it is refused."""
    residents = {"res-gable": {"path_map": {
        "/opt/disjorn": "/srv/disjorn-ro",
        "/home/resident": "/home/res-gable/resident-home",
    }}}
    with pytest.raises(ConfigError) as ei:
        _check("/home/res-gable/resident-home/SPECS", residents=residents)
    assert "/home/res-gable" in str(ei.value)


def test_declared_writable_root_is_refused(tmp_path):
    """[residents.<r>].writable_roots is the explicit escape hatch for volumes
    the convention cannot infer (a resident-rw mount outside /home). Declaring
    one makes specs_dir under it fatal even though the perms may look fine."""
    vol = tmp_path / "gable-work"
    (vol / "SPECS").mkdir(mode=0o755, parents=True)
    residents = {"res-gable": {"writable_roots": [str(vol)]}}
    with pytest.raises(ConfigError) as ei:
        _check(vol / "SPECS", residents=residents)
    assert "declared writable root" in str(ei.value)


# ------------------------------------------------------ RULE 2: permissions

def test_specs_dir_owned_by_a_resident_uid_is_refused(tmp_path):
    """Owned by a resident and owner-writable. Simulated by declaring OUR uid
    the resident and someone else the broker — the same stat() comparison the
    daemon makes with real res-* uids."""
    specs = tmp_path / "SPECS"
    specs.mkdir(mode=0o755)
    with pytest.raises(ConfigError) as ei:
        _check(specs, uid_map={ME: "res-gable"}, broker_uid=0)
    assert str(specs) in str(ei.value) and "owned by resident uid" in str(ei.value)


def test_resident_writable_parent_is_refused(tmp_path):
    """The subtler case: SPECS/ itself is locked down, but its PARENT is not —
    so a resident can rename SPECS aside and drop in its own."""
    parent = tmp_path / "mirror"
    specs = parent / "SPECS"
    specs.mkdir(parents=True)
    specs.chmod(0o555)                      # leaf: not writable by its owner
    parent.chmod(0o755)                     # parent: owner-writable
    try:
        with pytest.raises(ConfigError) as ei:
            _check(specs, uid_map={ME: "res-gable"}, broker_uid=0)
        assert str(parent) in str(ei.value)
    finally:
        specs.chmod(0o755)


def test_group_writable_path_whose_group_a_resident_is_in_is_refused(tmp_path):
    """Group-writable, and the group is one the resident belongs to. The gid
    resolver is injected, so this needs no real shared group."""
    specs = tmp_path / "SPECS"
    specs.mkdir()
    specs.chmod(0o770)                       # after mkdir: umask does not apply
    with pytest.raises(ConfigError) as ei:
        _check(specs, gids=lambda uid: {os.stat(specs).st_gid})
    assert "group-writable" in str(ei.value)


def test_group_writable_path_whose_group_no_resident_is_in_is_accepted(tmp_path):
    specs = tmp_path / "SPECS"
    specs.mkdir()
    specs.chmod(0o770)
    assert _check(specs, gids=lambda uid: {999999}) == str(specs.resolve())


def test_world_writable_specs_dir_is_refused(tmp_path):
    specs = tmp_path / "SPECS"
    specs.mkdir()
    specs.chmod(0o777)                       # after mkdir: umask does not apply
    with pytest.raises(ConfigError) as ei:
        _check(specs)
    assert "world-writable" in str(ei.value)


def test_sticky_world_writable_parent_is_allowed_but_sticky_leaf_is_not(tmp_path):
    """The /tmp shape. A sticky parent is safe — the kernel forbids renaming
    or deleting entries you do not own, so the next component cannot be
    swapped. A sticky LEAF is still fatal: creating a NEW file is allowed, and
    a new .md in SPECS/ is the whole attack."""
    parent = tmp_path / "sticky"
    specs = parent / "SPECS"
    specs.mkdir(parents=True, mode=0o755)
    parent.chmod(0o1777)
    assert os.stat(parent).st_mode & stat.S_ISVTX
    assert _check(specs) == str(specs.resolve())        # sticky parent: fine
    specs.chmod(0o1777)                                  # sticky leaf: not fine
    try:
        with pytest.raises(ConfigError) as ei:
            _check(specs)
        assert str(specs) in str(ei.value)
    finally:
        specs.chmod(0o755)
        parent.chmod(0o755)


def test_missing_specs_dir_is_refused(tmp_path):
    with pytest.raises(ConfigError) as ei:
        _check(tmp_path / "nope")
    assert "does not exist" in str(ei.value)


def test_specs_dir_that_is_a_file_is_refused(tmp_path):
    f = tmp_path / "SPECS"
    f.write_text("not a dir")
    with pytest.raises(ConfigError):
        _check(f)


# ------------------------------------------- wired into broker construction

def _config(tmp_path, specs_dir, *, uid=OTHER, extra_start=None):
    start = {"command": ["/bin/true"], "model": "m", "specs_dir": str(specs_dir)}
    start.update(extra_start or {})
    return {
        "broker": {"socket_path": str(tmp_path / "s.sock"),
                   "audit_log": str(tmp_path / "a.jsonl")},
        "uids": {str(uid): "res-gable"},
        "residents": {"res-gable": {"log_path": str(tmp_path / "l")}},
        "start_build": start,
    }


def test_broker_refuses_to_construct_with_a_resident_writable_specs_dir(tmp_path):
    """Fail at STARTUP, not per-request: plink sees this the moment the config
    is wrong, not the first time a resident builds."""
    cfg = _config(tmp_path, "/home/res-gable/resident-home/SPECS")
    with pytest.raises(ConfigError) as ei:
        Broker(cfg, str(tmp_path / "v.toml"))
    assert "/home/res-gable" in str(ei.value)


def test_broker_constructs_with_a_safe_specs_dir(tmp_path):
    specs = tmp_path / "SPECS"
    specs.mkdir(mode=0o755)
    broker = Broker(_config(tmp_path, specs), str(tmp_path / "v.toml"))
    assert broker.specs_dir_real == str(specs.resolve())


def test_start_build_section_without_specs_dir_is_refused(tmp_path):
    """Fail closed on omission: a [start_build] section means someone intends
    to run builds, so a missing specs_dir is drift, not a default."""
    cfg = _config(tmp_path, tmp_path / "SPECS")
    del cfg["start_build"]["specs_dir"]
    with pytest.raises(ConfigError) as ei:
        Broker(cfg, str(tmp_path / "v.toml"))
    assert "specs_dir" in str(ei.value)


def test_no_start_build_section_is_not_checked(tmp_path):
    """A broker with no start_build config at all still constructs — the verb
    fails closed at request time (internal: not configured)."""
    cfg = _config(tmp_path, tmp_path / "SPECS")
    del cfg["start_build"]
    broker = Broker(cfg, str(tmp_path / "v.toml"))
    assert broker.specs_dir_real is None
    with pytest.raises(Exception) as ei:
        broker._verb_start_build("res-gable", {"spec": "x.md"})
    assert "specs_dir" in str(ei.value)


def test_specs_dir_used_is_the_specs_dir_verified(tmp_path):
    """Mutating start_build['specs_dir'] after construction must NOT move the
    gate: the verb reads the realpath proven safe at startup."""
    specs = tmp_path / "SPECS"
    specs.mkdir(mode=0o755)
    broker = Broker(_config(tmp_path, specs), str(tmp_path / "v.toml"))
    broker.start_build["specs_dir"] = "/home/res-gable/resident-home/SPECS"
    assert broker._specs_dir() == str(specs.resolve())


def test_main_exits_nonzero_on_unsafe_config(tmp_path, capsys):
    """The daemon entry point must exit LOUD and non-zero — no degraded start
    with start-build quietly missing."""
    cfg_path = tmp_path / "broker.toml"
    cfg_path.write_text(
        f'[broker]\nsocket_path = "{tmp_path / "s.sock"}"\n'
        f'audit_log = "{tmp_path / "a.jsonl"}"\n\n'
        f'[uids]\n"{OTHER}" = "res-gable"\n\n'
        f'[start_build]\ncommand = ["/bin/true"]\nmodel = "m"\n'
        f'specs_dir = "/home/res-gable/resident-home/SPECS"\n')
    rc = main(["--config", str(cfg_path), "--verbs", str(tmp_path / "v.toml")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSING TO START" in err and "/home/res-gable" in err


# ------------------------------------------------------------ the template

def test_shipped_template_specs_dir_is_the_readonly_mirror():
    """The shipped default must be the plink-gated RO mirror, and must not sit
    in any resident home named by the template's own [uids]/[residents]."""
    tmpl_dir = Path(__file__).resolve().parent.parent
    with open(tmpl_dir / "broker.toml", "rb") as fh:
        tmpl = tomllib.load(fh)
    specs_dir = tmpl["start_build"]["specs_dir"]
    assert specs_dir == "/srv/disjorn-ro/SPECS"
    for name in tmpl["residents"]:
        assert not specs_dir.startswith(f"/home/{name}/")
