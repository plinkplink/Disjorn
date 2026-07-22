"""Per-resident protected paths + inert/protected precedence.

These tests run against the SHIPPED harness/classifier/protected-paths.toml,
not a fixture config: the finding they cover is a config hole, so a test
against a hand-written fixture config would prove nothing.

The hole (found at the keyboard 2026-07-22): the per-resident section of
protected-paths.toml was never filled in after WP-H5/H9/H11 landed, while
`[inert].patterns` contains `*.md`. A resident's spine is markdown. So a diff
in which a resident rewrote their OWN spine — `bots/fable/spine/*.md`, the
source the kernel is assembled from — classified as an inert docs change and
skipped the human gate entirely, contradicting both AGENTHOOD.md ("a
resident's own code and prompt are always Tier 2") and
house_memory/bootstrap.py ("edit the spine, not this file" / witnessed,
merged).

SCOPE, stated so nobody over-reads these greens: this closes the DIFF path.
`RESIDENT_SPINE_DIR` still resolves to a directory the resident owns and can
write, so a resident editing their spine in place produces no diff for the
classifier to see. That wall is a filesystem/permissions change outside this
package.
"""
from pathlib import Path

import pytest

from classify_diff import classify, is_inert, is_protected, load_config

from test_classify_diff import (  # noqa: F401  (git/write/commit_all helpers)
    GATES_PASS,
    commit_all,
    git,
    write,
)

SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "protected-paths.toml"

SPINE_ENTRY = (
    "---\nname: load-bearing-walls\nkernel: true\n---\n"
    "The things that must not move.\n"
)


@pytest.fixture
def cfg():
    return load_config(str(SHIPPED_CONFIG))


@pytest.fixture
def resident_repo(tmp_path):
    """A repo laid out like the gatehouse view: bots/<resident>/... .

    Mirrors the real trees:
      bots/fable      — spine/*.md, GENESIS.md, PICKUP.md
      bots/claudette  — disjorn_bot.py, core.py, bot.py, config.py, memory/...
    """
    r = tmp_path / "resident-fixture"
    r.mkdir()
    git(r, "init", "-b", "main")
    for name in ("00-kernel", "10-people", "20-load-bearing-walls",
                 "30-build-rhythm", "40-cautions", "50-genesis"):
        write(r, f"bots/fable/spine/{name}.md", SPINE_ENTRY)
    write(r, "bots/fable/GENESIS.md", "# Genesis\n")
    write(r, "bots/fable/PICKUP.md", "# Pickup\n")
    write(r, "bots/claudette/disjorn_bot.py", "def run():\n    return 1\n")
    write(r, "bots/claudette/core.py", "def core():\n    return 1\n")
    write(r, "bots/claudette/bot.py", "def bot():\n    return 1\n")
    write(r, "bots/claudette/config.py", "TOKEN_ENV = 'X'\n")
    write(r, "bots/claudette/requirements.txt", "discord.py==2.4.0\n")
    write(r, "bots/claudette/memory/store.py", "def store():\n    return 1\n")
    write(r, "bots/claudette/services/api.py", "def api():\n    return 1\n")
    write(r, "docs/notes.md", "ordinary docs\n")
    write(r, "README.md", "# fixture\n")
    git(r, "add", "-A")
    git(r, "commit", "-m", "baseline")
    git(r, "tag", "base")
    return r


@pytest.fixture
def own_repo(tmp_path):
    """The resident's OWN repo root — bots/fable IS a git repo, so a diff
    classified there has no bots/<name>/ prefix."""
    r = tmp_path / "own-fixture"
    r.mkdir()
    git(r, "init", "-b", "main")
    write(r, "spine/20-load-bearing-walls.md", SPINE_ENTRY)
    write(r, "GENESIS.md", "# Genesis\n")
    write(r, "core.py", "def core():\n    return 1\n")
    write(r, "memory/store.py", "def store():\n    return 1\n")
    write(r, ".claude/CLAUDE.md", "# assembled kernel\n")
    write(r, "MEMORY.md", "# spine index\n")
    write(r, "notes.md", "scratch\n")
    git(r, "add", "-A")
    git(r, "commit", "-m", "baseline")
    git(r, "tag", "base")
    return r


def classify_repo(r):
    return classify(str(r), str(SHIPPED_CONFIG), range_spec="base..HEAD",
                    gates=GATES_PASS)


# ==========================================================================
# The four that matter most (per the finding)
# ==========================================================================


def test_spine_edit_is_tier2_not_tier0(resident_repo):
    """THE hole: a resident rewriting their own spine entry. Markdown, on the
    inert allowlist — and it must still stop at the human gate."""
    write(resident_repo, "bots/fable/spine/20-load-bearing-walls.md",
          SPINE_ENTRY + "\nAlso: the classifier is advisory.\n")
    commit_all(resident_repo)
    result = classify_repo(resident_repo)
    assert result["tier"] == 2, result["reasons"]
    assert "bots/fable/spine/20-load-bearing-walls.md" in result["protected_hits"]


def test_new_spine_entry_creation_is_tier2(resident_repo):
    """Adding a NEW spine file is a prompt edit too — this is why the spine is
    a `dirs` entry (dir entries cover creation) and not a list of filenames."""
    write(resident_repo, "bots/fable/spine/60-new.md",
          "---\nname: new\nkernel: true\n---\nnew rule\n")
    commit_all(resident_repo)
    result = classify_repo(resident_repo)
    assert result["tier"] == 2, result["reasons"]
    assert "bots/fable/spine/60-new.md" in result["protected_hits"]
    assert any("created at protected path" in x for x in result["reasons"])


def test_claudette_adapter_is_tier2(resident_repo):
    write(resident_repo, "bots/claudette/disjorn_bot.py",
          "def run():\n    return 2\n")
    commit_all(resident_repo)
    result = classify_repo(resident_repo)
    assert result["tier"] == 2
    assert "bots/claudette/disjorn_bot.py" in result["protected_hits"]


def test_claudette_core_is_tier2(resident_repo):
    write(resident_repo, "bots/claudette/core.py", "def core():\n    return 2\n")
    commit_all(resident_repo)
    result = classify_repo(resident_repo)
    assert result["tier"] == 2
    assert "bots/claudette/core.py" in result["protected_hits"]


# ==========================================================================
# inert vs protected precedence — protection must win
# ==========================================================================


def test_spine_entry_matches_both_inert_and_protected(cfg):
    """The precedence question, made explicit at the predicate level: this
    path IS inert-matching (`*.md`) AND protected. Both are true at once."""
    p = "bots/fable/spine/20-load-bearing-walls.md"
    assert is_inert(p, cfg) is True
    assert is_protected(p, cfg) is True


def test_protected_beats_inert_in_the_tier_decision(resident_repo):
    """...and protection wins. classify() decides protection BEFORE it
    consults the inert allowlist; if that order is ever inverted, this diff
    silently becomes a Tier-0 auto-apply. (Verified: the pre-existing code
    already ordered it correctly — this test pins it, it does not fix it.)"""
    write(resident_repo, "bots/fable/spine/00-kernel.md", SPINE_ENTRY + "edit\n")
    commit_all(resident_repo)
    result = classify_repo(resident_repo)
    assert result["tier"] == 2
    assert not any("inert paths only" in x for x in result["reasons"])


def test_ordinary_markdown_is_still_tier0(resident_repo):
    """Guard: the fix must not reclassify ordinary docs traffic. `*.md` is
    still an inert allowlist for everything that is not prompt-bearing."""
    write(resident_repo, "docs/notes.md", "expanded notes\n")
    write(resident_repo, "README.md", "# fixture v2\n")
    commit_all(resident_repo)
    result = classify_repo(resident_repo)
    assert result["tier"] == 0
    assert result["protected_hits"] == []


def test_mixed_spine_and_docs_diff_is_entirely_tier2(resident_repo):
    """No smuggling: a spine edit hidden inside a docs-only-looking diff."""
    write(resident_repo, "docs/notes.md", "expanded notes\n")
    write(resident_repo, "bots/fable/spine/40-cautions.md", SPINE_ENTRY + "x\n")
    commit_all(resident_repo)
    result = classify_repo(resident_repo)
    assert result["tier"] == 2
    assert any("mixed diff" in x for x in result["reasons"])


# ==========================================================================
# the rest of the per-resident surface
# ==========================================================================


@pytest.mark.parametrize("path", [
    "bots/fable/GENESIS.md",
    "bots/fable/PICKUP.md",
    "bots/fable/spine/00-kernel.md",
    "bots/claudette/bot.py",
    "bots/claudette/config.py",
    "bots/claudette/requirements.txt",
    "bots/claudette/PROMPT-PROPOSAL.md",
    "bots/claudette/memory/store.py",
    "bots/claudette/memory/schema.py",      # creation inside memory/
    "bots/claudette/services/api.py",
    "bots/claudette/scripts/run.sh",
])
def test_resident_surface_is_protected(cfg, path):
    assert is_protected(path, cfg), path


@pytest.mark.parametrize("path", [
    "spine/20-load-bearing-walls.md",   # resident's own repo root view
    "spine/60-brand-new.md",
    "GENESIS.md",
    "PICKUP.md",
    "core.py",
    "disjorn_bot.py",
    "bot.py",
    "config.py",
    "memory/store.py",
    ".claude/CLAUDE.md",                # assembled kernel
    "CLAUDE.md",
    "MEMORY.md",                        # generated spine index
    "harness/cc/config-template/CLAUDE.md",   # the kernel template in-repo
])
def test_own_repo_view_is_protected(cfg, path):
    """The classifier cannot know which repo it was pointed at, so both the
    gatehouse view (bots/<name>/...) and the resident's own repo root view
    must be covered."""
    assert is_protected(path, cfg), path


def test_own_repo_spine_edit_is_tier2(own_repo):
    write(own_repo, "spine/20-load-bearing-walls.md", SPINE_ENTRY + "edit\n")
    commit_all(own_repo)
    result = classify_repo(own_repo)
    assert result["tier"] == 2
    assert "spine/20-load-bearing-walls.md" in result["protected_hits"]


def test_own_repo_kernel_edit_is_tier2(own_repo):
    write(own_repo, ".claude/CLAUDE.md", "# assembled kernel\nrewritten\n")
    commit_all(own_repo)
    result = classify_repo(own_repo)
    assert result["tier"] == 2


def test_own_repo_scratch_markdown_is_still_tier0(own_repo):
    """Guard: a resident's non-prompt markdown is still inert."""
    write(own_repo, "notes.md", "scratch, updated\n")
    commit_all(own_repo)
    assert classify_repo(own_repo)["tier"] == 0


@pytest.mark.parametrize("path", [
    "docs/notes.md",
    "README.md",
    "server/app/services/util.py",
    "client/src/api.ts",
    "harness/house_memory/house_memory/spine.py",  # NOT a spine directory
])
def test_unrelated_paths_are_not_swept_up(cfg, path):
    """False-positive control on the new `*spine/*` / basename patterns: the
    house_memory spine LIBRARY is ordinary code, not a resident's spine."""
    assert not is_protected(path, cfg), path
