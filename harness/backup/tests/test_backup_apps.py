"""The apps in a snapshot: bundled and staged by snapshot.sh, checked by drill.sh, against a tmp /srv."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import apps
from test_backup_drill import house, only_snapshot, posts, restored, run  # noqa: F401

LIVE, DRAFT = "aaaaaaaaaaaa", "bbbbbbbbbbbb"


def _git(env, git_dir, *args, work_tree=None):
    where = [f"--git-dir={git_dir}"] + ([f"--work-tree={work_tree}"] if work_tree else [])
    return subprocess.run(["git", *where, *args], env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _app(house, app, *, live):
    """The split layout the launcher makes: git dir, work tree and web roots under three roots."""
    tmp, env = house["tmp"], house["env"]
    git_dir, work = tmp / "apps-git" / f"{app}.git", tmp / "apps" / app
    work.mkdir(parents=True)
    _git(env, git_dir, "init", "-q", "-b", "main", work_tree=work)
    (work / "index.html").write_text(f"<h1>{app}</h1>\n")
    (work / "vendor").mkdir()
    (work / "vendor/lib.js").write_text("lib\n")
    _git(env, git_dir, "add", "-A", work_tree=work)
    _git(env, git_dir, "commit", "-qm", "turn 1", work_tree=work)
    www = tmp / "apps-www" / app
    for root in ("preview", "live", "live.prev") if live else ("preview",):
        (www / root / "vendor").mkdir(parents=True)
        (www / root / "index.html").write_text(f"<h1>{app}</h1>\n")
        (www / root / "vendor/lib.js").write_text("lib\n")
    if live:
        _git(env, git_dir, "update-ref", "refs/disjorn/live", "HEAD")
    return git_dir, www


@pytest.fixture
def srv(house):
    house["live"] = _app(house, LIVE, live=True)
    house["draft"] = _app(house, DRAFT, live=False)
    (house["tmp"] / "apps-www/.staging").mkdir()
    return house


def snapshot(house):
    r = run(house, "snapshot.sh")
    assert r.returncode == 0, r.stderr
    snap = only_snapshot(house)
    staged = restored(house, snap, house["stage"])
    return r, staged, json.loads((staged / "manifest.json").read_text())


def tags(house):
    return json.loads(next((house["tmp"] / "repo/snaps").glob("*.json")).read_text())["tags"]


def test_each_app_is_bundled_with_its_live_ref_and_counted_in_the_manifest(srv):
    _, staged, m = snapshot(srv)
    assert set(m["apps"]["apps"]) == {LIVE, DRAFT} and m["apps"]["warnings"] == {}
    git_dir, _ = srv["live"]
    head = _git(srv["env"], git_dir, "rev-parse", "HEAD")
    bundle = staged / "apps" / LIVE / "repo.bundle"
    size = len(f"<h1>{LIVE}</h1>\n") + len("lib\n")
    assert m["apps"]["apps"][LIVE] == {
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "head": head, "live_commit": head,
        "live": {"files": 2, "bytes": size}, "live_prev": {"files": 2, "bytes": size},
        "preview": {"files": 2, "bytes": size}}
    heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], capture_output=True,
                           text=True, check=True).stdout
    assert f"{head} refs/disjorn/live" in heads and f"{head} refs/heads/main" in heads
    draft = m["apps"]["apps"][DRAFT]
    assert draft["live"] is None and draft["live_commit"] is None and draft["preview"]["files"] == 2
    assert (staged / "apps" / LIVE / "live/vendor/lib.js").read_text() == "lib\n"
    assert "index.html" not in json.dumps(m) and "lib.js" not in json.dumps(m)
    assert tags(srv) == ["disjorn-nightly"]


def test_drill_passes_and_says_what_it_checked_of_the_apps(srv):
    snapshot(srv)
    r = run(srv, "drill.sh")
    assert r.returncode == 0, r.stderr + "\n".join(posts(srv))
    [post] = posts(srv)
    assert post.startswith("backup drill PASS")
    assert post.endswith("apps: 2 bundles verified, 1 live roots matched")


def _corrupt_bundle(staged):
    (staged / "apps" / LIVE / "repo.bundle").write_bytes(b"not a bundle")


def _drop_live_file(staged):
    (staged / "apps" / LIVE / "live/vendor/lib.js").unlink()


def _drop_bundle(staged):
    (staged / "apps" / DRAFT / "repo.bundle").unlink()


def _swap_bundles(staged):
    (staged / "apps" / DRAFT / "repo.bundle").write_bytes(
        (staged / "apps" / LIVE / "repo.bundle").read_bytes())


def _forget_apps(staged):
    m = json.loads((staged / "manifest.json").read_text())
    del m["apps"]
    (staged / "manifest.json").write_text(json.dumps(m))


@pytest.mark.parametrize("tamper, says", [
    (_corrupt_bundle, f"app {LIVE}: bundle verify failed"),
    (_drop_live_file, f"app {LIVE}: live holds 1 files, 22 bytes, manifest says 2 files, 26 bytes"),
    (_drop_bundle, f"app {DRAFT}: no bundle in the snapshot"),
    (_swap_bundles, f"app {DRAFT}: bundle sha256 differs from manifest"),
    (_forget_apps, "the manifest has no apps section"),
])
def test_drill_goes_red_on_a_tampered_app(srv, tamper, says):
    _, staged, _ = snapshot(srv)
    tamper(staged)
    r = run(srv, "drill.sh")
    assert r.returncode == 1
    [post] = posts(srv)
    assert post.startswith("backup drill RED") and says in post, post


def test_drill_red_when_the_bundle_does_not_hold_the_recorded_head(srv):
    _, staged, m = snapshot(srv)
    m["apps"]["apps"][LIVE]["head"] = "0" * 40
    (staged / "manifest.json").write_text(json.dumps(m))
    assert run(srv, "drill.sh").returncode == 1
    assert f"app {LIVE}: HEAD is " in posts(srv)[0]


def test_a_symlink_under_a_web_root_is_never_followed_out_of_the_tree(srv):
    outside = srv["tmp"] / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("TOP SECRET\n")
    _, www = srv["live"]
    (www / "live/leak.txt").symlink_to(outside / "passwd")
    (www / "live/leakdir").symlink_to(outside, target_is_directory=True)
    (srv["draft"][1] / "live").symlink_to(outside, target_is_directory=True)

    r, staged, m = snapshot(srv)
    assert m["apps"]["apps"][LIVE]["live"]["files"] == 2
    assert not list((staged / "apps").rglob("leak*")) and not list((staged / "apps").rglob("passwd"))
    for p in only_snapshot(srv).rglob("*"):
        assert not p.is_symlink()
        assert not (p.is_file() and b"TOP SECRET" in p.read_bytes()), p
    assert m["apps"]["warnings"] == {DRAFT: "live: Not a directory"}
    draft = m["apps"]["apps"][DRAFT]
    assert draft["live"] is None and draft["bundle_sha256"] and draft["preview"]["files"] == 2
    assert f"WARNING {DRAFT}" in r.stderr


def test_an_app_deleted_mid_backup_is_skipped_and_the_others_are_recorded(srv):
    git_dir, www = srv["live"]
    wrapper = srv["tmp"] / "bin/git"
    wrapper.write_text(f'#!/bin/sh\ncase "$*" in *{LIVE}.git*bundle*) rm -rf {git_dir} {www};; esac\n'
                       'exec /usr/bin/git "$@"\n')
    wrapper.chmod(0o755)
    r, staged, m = snapshot(srv)
    assert set(m["apps"]["apps"]) == {DRAFT} and m["apps"]["warnings"] == {}
    assert f"apps: {LIVE} vanished during the backup" in r.stderr
    assert not (staged / "apps" / LIVE).exists()
    assert tags(srv) == ["disjorn-nightly"]
    assert run(srv, "drill.sh").returncode == 0
    assert "apps: 1 bundles verified, 0 live roots matched" in posts(srv)[0]


def test_a_broken_git_dir_is_a_warning_and_the_coverage_is_called_partial(srv):
    half, broken = "cccccccccccc", "dddddddddddd"
    (srv["tmp"] / "apps-git" / f"{half}.git/objects").mkdir(parents=True)
    git_dir, _ = _app(srv, broken, live=True)
    for obj in (git_dir / "objects").glob("??/*"):
        obj.unlink()

    r, staged, m = snapshot(srv)
    assert m["apps"]["warnings"] == {half: "git dir has no readable HEAD",
                                     broken: "git could not bundle the repository"}
    assert f"apps: {broken}.git: git said: " in r.stderr
    assert f"apps: WARNING {half}: git dir has no readable HEAD" in r.stderr
    assert {LIVE, DRAFT} <= set(m["apps"]["apps"])
    assert m["apps"]["apps"][broken]["bundle_sha256"] is None
    assert m["apps"]["apps"][broken]["live"]["files"] == 2
    assert not (staged / "apps" / broken / "repo.bundle").exists()
    assert tags(srv) == ["disjorn-nightly", "apps-partial"]

    r = run(srv, "drill.sh")
    assert r.returncode == 0, r.stderr
    [post] = posts(srv)
    assert post.startswith("backup drill PARTIAL on snapshot")
    assert "apps: 2 bundles verified, 2 live roots matched; APPS COVERAGE PARTIAL at snapshot time: " in post
    assert f"{half} (git dir has no readable HEAD)" in post

    r = run(srv, "freshness.sh")
    assert r.returncode == 0, r.stderr
    assert posts(srv)[1].startswith("backup freshness WARNING") and "apps coverage was partial" in posts(srv)[1]


def test_a_missing_apps_root_is_partial_coverage_not_zero_apps(srv):
    srv["env"]["APPS_GIT_ROOT"] = str(srv["tmp"] / "nowhere")
    r, _, m = snapshot(srv)
    assert m["apps"]["warnings"] == {"nowhere": "root: No such file or directory"}
    assert m["apps"]["apps"][LIVE]["bundle_sha256"] is None and m["apps"]["apps"][LIVE]["live"]
    assert "apps-partial" in tags(srv)


def test_an_app_with_no_commits_is_recorded_without_a_bundle(srv):
    empty = "eeeeeeeeeeee"
    _git(srv["env"], srv["tmp"] / "apps-git" / f"{empty}.git", "init", "-q", "-b", "main",
         work_tree=srv["tmp"] / "apps" / empty)
    _, staged, m = snapshot(srv)
    assert m["apps"]["apps"][empty]["bundle_sha256"] is None and m["apps"]["warnings"] == {}
    assert not (staged / "apps" / empty / "repo.bundle").exists()
    assert run(srv, "drill.sh").returncode == 0


def test_git_reads_a_seat_owned_repo_as_the_seat_when_the_backup_is_root(srv, tmp_path, monkeypatch):
    log = tmp_path / "runuser.log"
    runuser = srv["tmp"] / "bin/runuser"
    runuser.write_text(f'#!/bin/sh\necho "$@" >> {log}\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    runuser.chmod(0o755)
    for key in ("PATH", "GIT_CONFIG_GLOBAL"):
        monkeypatch.setenv(key, srv["env"][key])
    monkeypatch.setattr(apps.os, "geteuid", lambda: 0)
    facts = apps.stage(srv["tmp"] / "apps-git", srv["tmp"] / "apps-www", tmp_path / "out")
    assert facts["warnings"] == {} and facts["apps"][LIVE]["head"]
    me = __import__("pwd").getpwuid(os.getuid()).pw_name
    calls = log.read_text().splitlines()
    assert len(calls) == 4 and all(c.startswith(f"-u {me} -- git ") for c in calls), calls


def test_one_app_comes_back_from_its_bundle_by_the_readme_recipe(srv, tmp_path):
    _, staged, m = snapshot(srv)
    env, git_dir, work = srv["env"], tmp_path / f"{LIVE}.git", tmp_path / "work"
    _git(env, git_dir, "init", "-q", "--bare", "-b", "main")
    _git(env, git_dir, "fetch", "-q", str(staged / "apps" / LIVE / "repo.bundle"), "+refs/*:refs/*")
    _git(env, git_dir, "config", "core.bare", "false")
    work.mkdir()
    _git(env, git_dir, "checkout", "-q", "-f", work_tree=work)
    want = m["apps"]["apps"][LIVE]
    assert _git(env, git_dir, "rev-parse", "HEAD") == want["head"]
    assert _git(env, git_dir, "rev-parse", "refs/disjorn/live") == want["live_commit"]
    assert (work / "vendor/lib.js").read_text() == "lib\n"
    assert _git(env, git_dir, "status", "--porcelain", work_tree=work) == ""
