"""run-gates.sh: what it refuses, and the four lines it is allowed to print.

The broker parses this script's stdout and merges on the answer, so stdout is
a contract: four GATE lines, in order, and nothing else. Real git repos in
tmp_path — whether a branch exists and whether it touched client/ are git's
questions — and a fake podman, because the container is the one thing these
tests do not need.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
RUN_GATES = CC_DIR / "run-gates.sh"

NAME = "gable"
SLUG = "2026-09-20-a-slug"
BRANCH = f"loop/{SLUG}"

FAKE_PODMAN = r"""#!/usr/bin/env bash
printf '%s\0' "$@" > "$DUMP_DIR/argv"
[ -n "${FAKE_GATE_STDOUT:-}" ] && printf '%s\n' "$FAKE_GATE_STDOUT"
echo "container noise" >&2
exit "${FAKE_PODMAN_RC:-0}"
"""

GREEN = "GATE tests pass\nGATE typecheck skipped\nGATE build skipped"


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True,
                          capture_output=True)


@pytest.fixture()
def rig(tmp_path):
    """A gatehouse with main and one loop branch, plus run()."""
    gatehouse = tmp_path / "gatehouse"
    dump = tmp_path / "dump"
    bindir = tmp_path / "bin"
    runs = tmp_path / "gate-runs"
    for d in (gatehouse, dump, bindir, runs):
        d.mkdir()
    bare = gatehouse / "disjorn.git"
    git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git("clone", "--quiet", str(bare), str(seed))
    (seed / "README").write_text("base\n")
    git("add", "README", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=seed)
    git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=seed)

    podman = bindir / "podman"
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)

    class Rig:
        pass

    rig = Rig()
    rig.tmp = tmp_path
    rig.gatehouse = gatehouse
    rig.bare = bare
    rig.seed = seed
    rig.dump = dump

    def branch(*paths: str):
        """Publish loop/<slug> touching the given paths."""
        git("checkout", "--quiet", "-b", BRANCH, cwd=seed)
        for rel in paths:
            f = seed / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("change\n")
            git("add", rel, cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "w", cwd=seed)
        git("push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=seed)

    def run(*args, stdout_lines: str = GREEN, rc: int = 0,
            node_modules: str | None = None, gatehouse_dir: str | None = None):
        env = dict(os.environ)
        env.update(
            PATH=f"{bindir}:{env['PATH']}",
            DUMP_DIR=str(dump),
            RESIDENT_IMAGE="localhost/disjorn-resident:test",
            RESIDENT_GATEHOUSE=gatehouse_dir or str(gatehouse),
            RESIDENT_GATE_RUNS=str(runs),
            RESIDENT_CLIENT_NODE_MODULES=node_modules or str(tmp_path / "absent"),
            FAKE_GATE_STDOUT=stdout_lines,
            FAKE_PODMAN_RC=str(rc),
        )
        return subprocess.run(["bash", str(RUN_GATES), *args],
                              capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=120)

    rig.branch = branch
    rig.run = run
    rig.runs = runs
    return rig


def gate_lines(cp) -> list[str]:
    return cp.stdout.splitlines()


def podman_argv(rig) -> list[str]:
    raw = (rig.dump / "argv").read_bytes().decode()
    return [a for a in raw.split("\0") if a]


# ======================================================================
# the arguments
# ======================================================================

def test_the_arity_is_exactly_two(rig):
    for args in ([], [NAME], [NAME, SLUG, "extra"]):
        cp = rig.run(*args)
        assert cp.returncode == 2, args
        assert "usage" in cp.stderr


def test_a_hostile_slug_never_reaches_git_or_podman(rig):
    for slug in ["../../etc/passwd", "2026-09-20-a b", "2026-13-45-x",
                 "2026-09-20-X", "gates", "2026-09-20-" + "a" * 60]:
        cp = rig.run(NAME, slug)
        assert cp.returncode == 2, slug
        assert not (rig.dump / "argv").exists()


def test_a_hostile_resident_is_refused(rig):
    for name in ["res-gable", "../gable", "Gable", ""]:
        assert rig.run(name, SLUG).returncode == 2, name


def test_a_missing_gatehouse_refuses_before_the_clone(rig):
    cp = rig.run(NAME, SLUG, gatehouse_dir=str(rig.tmp / "nowhere"))
    assert cp.returncode == 2 and "gatehouse missing" in cp.stderr


def test_a_branch_that_does_not_exist_refuses(rig):
    cp = rig.run(NAME, SLUG)
    assert cp.returncode == 2
    assert "no such branch" in cp.stderr


# ======================================================================
# the GATE-line contract
# ======================================================================

def test_stdout_is_four_gate_lines_in_order_and_nothing_else(rig):
    rig.branch("server/app/thing.py")
    cp = rig.run(NAME, SLUG)
    assert gate_lines(cp) == ["GATE tests pass", "GATE typecheck skipped",
                             "GATE build skipped", "GATE exit 0"]
    assert cp.returncode == 0
    # Everything the run says about itself lands on the other stream.
    assert "container noise" in cp.stderr


def test_a_red_suite_is_a_red_exit(rig):
    rig.branch("server/app/thing.py")
    cp = rig.run(NAME, SLUG, stdout_lines="GATE tests fail", rc=1)
    assert gate_lines(cp) == ["GATE tests fail", "GATE typecheck skipped",
                             "GATE build skipped", "GATE exit 1"]
    assert cp.returncode == 1


def test_a_container_that_says_nothing_is_red(rig):
    """Silence is never read as success: a gate that printed no verdict did
    not run one."""
    rig.branch("server/app/thing.py")
    cp = rig.run(NAME, SLUG, stdout_lines="", rc=125)
    assert gate_lines(cp)[0] == "GATE tests fail"
    assert cp.returncode == 1


# ======================================================================
# the client gates
# ======================================================================

def test_a_branch_that_leaves_client_alone_skips_and_mounts_nothing(rig):
    rig.branch("server/app/thing.py")
    node_modules = rig.tmp / "node_modules"
    node_modules.mkdir()
    cp = rig.run(NAME, SLUG, node_modules=str(node_modules))
    assert "GATE typecheck skipped" in cp.stdout
    argv = podman_argv(rig)
    assert not any("node_modules" in a for a in argv)
    assert "GATE_CLIENT=0" in argv


def test_a_client_change_mounts_the_toolchain_read_only(rig):
    rig.branch("client/src/app.tsx")
    node_modules = rig.tmp / "node_modules"
    node_modules.mkdir()
    cp = rig.run(NAME, SLUG, node_modules=str(node_modules),
                 stdout_lines="GATE tests pass\nGATE typecheck pass\nGATE build pass")
    assert gate_lines(cp)[:3] == ["GATE tests pass", "GATE typecheck pass",
                                  "GATE build pass"]
    argv = podman_argv(rig)
    assert f"{node_modules}:/work/client/node_modules:ro" in argv
    assert "GATE_CLIENT=1" in argv


def test_a_client_change_with_no_toolchain_is_red_not_skipped(rig):
    rig.branch("client/src/app.tsx")
    cp = rig.run(NAME, SLUG)
    assert gate_lines(cp) == ["GATE tests pass", "GATE typecheck fail",
                              "GATE build fail", "GATE exit 1"]
    assert cp.returncode == 1


# ======================================================================
# the container and the checkout
# ======================================================================

def test_the_container_has_no_network_and_the_resident_userns(rig):
    rig.branch("server/app/thing.py")
    rig.run(NAME, SLUG)
    argv = podman_argv(rig)
    assert argv[:2] == ["run", "--rm"]
    assert "none" in argv and "--network" in argv
    assert "keep-id:uid=1000,gid=1000" in argv
    assert "localhost/disjorn-resident:test" in argv


def test_the_checkout_is_the_branch_tip_and_is_removed_afterwards(rig):
    rig.branch("server/app/thing.py")
    tip = git("rev-parse", f"refs/heads/{BRANCH}", cwd=rig.bare).stdout.strip()
    probe = rig.tmp / "probe"
    podman = rig.tmp / "bin" / "podman"
    podman.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\0" "$@" > "$DUMP_DIR/argv"\n'
        'for a in "$@"; do case "$a" in *:/work) '
        f'git -C "${{a%%:*}}" rev-parse HEAD > {probe} ;; esac; done\n'
        '[ -n "${FAKE_GATE_STDOUT:-}" ] && printf "%s\\n" "$FAKE_GATE_STDOUT"\n'
        "exit 0\n")
    podman.chmod(0o755)
    cp = rig.run(NAME, SLUG)
    assert cp.returncode == 0
    assert probe.read_text().strip() == tip
    assert list(rig.runs.iterdir()) == []
