"""WP-H4 classifier tests: fixture git repos in tmpdirs, one per scenario."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from classify_diff import classify, load_config

GATES_PASS = {"tests": True, "typecheck": True, "build": True}
GATES_FAIL = {"tests": False, "typecheck": True, "build": True}

CONFIG = """
[protected]
files = [
    "server/app/privacy.py",
    "server/app/routers/auth.py",
    "server/app/ws.py",
    "server/cli.py",
    "server/requirements.txt",
    "client/package.json",
    "client/package-lock.json",
    "sdk/pyproject.toml",
]
dirs = [
    "server/app/migrations",
    "deploy",
    "sdk/disjorn_sdk",
    "client/src/protected",
]
patterns = [".env*"]

[inert]
patterns = ["*.md", "docs/*", "client/src/*.css", "client/public/*"]

[limits]
size_cap = 150
daily_auto_apply_budget = 12
"""

PRIVACY_PY = "import json\n\n\ndef check(x):\n    return json.dumps(x)\n"


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin",
        },
    )


@pytest.fixture
def repo(tmp_path):
    """Fixture repo mirroring the real layout, with one baseline commit."""
    r = tmp_path / "fixture"
    r.mkdir()
    git(r, "init", "-b", "main")
    write(r, "README.md", "# fixture\n")
    write(r, "docs/notes.md", "notes\n")
    write(r, "server/app/privacy.py", PRIVACY_PY)
    write(r, "server/app/ws.py", "import json\n\n\ndef fanout(msg):\n    return msg\n")
    write(r, "server/app/services/util.py", "def util():\n    return 1\n")
    write(r, "server/app/routers/__init__.py", "")
    write(
        r,
        "client/src/protected/gate.ts",
        "import { api } from '../api';\n\nexport function gate() {\n  return api;\n}\n",
    )
    write(r, "client/src/api.ts", "export const api = 1;\n")
    write(r, "client/src/app.css", "body { margin: 0; }\n")
    cfg = tmp_path / "protected-paths.toml"
    cfg.write_text(CONFIG)
    git(r, "add", "-A")
    git(r, "commit", "-m", "baseline")
    git(r, "tag", "base")
    return r, cfg


def write(repo, rel, content):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def commit_all(r, msg="change"):
    git(r, "add", "-A")
    git(r, "commit", "-m", msg)


def run(repo_cfg, gates=GATES_PASS, staged=False):
    r, cfg = repo_cfg
    if staged:
        return classify(str(r), str(cfg), staged=True, gates=gates)
    return classify(str(r), str(cfg), range_spec="base..HEAD", gates=gates)


# -- Tier 0 -----------------------------------------------------------------


def test_docs_only_diff_is_tier0(repo):
    r, _ = repo
    write(r, "docs/notes.md", "notes, expanded\n")
    write(r, "README.md", "# fixture v2\n")
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 0
    assert result["protected_hits"] == []
    assert result["stats"]["files"] == 2


# -- Tier 1 -----------------------------------------------------------------


def test_small_code_diff_passing_gates_is_tier1(repo):
    r, _ = repo
    write(r, "server/app/services/util.py", "def util():\n    return 2\n")
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 1
    assert result["protected_hits"] == []


# -- Tier 2: protected touches ---------------------------------------------


def test_touch_privacy_py_is_tier2(repo):
    r, _ = repo
    write(r, "server/app/privacy.py", PRIVACY_PY + "\n# tweak\n")
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert "server/app/privacy.py" in result["protected_hits"]


def test_mixed_diff_is_entirely_tier2(repo):
    r, _ = repo
    write(r, "server/app/privacy.py", PRIVACY_PY + "\n# tweak\n")
    write(r, "server/app/services/util.py", "def util():\n    return 3\n")
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert any("mixed diff" in reason for reason in result["reasons"])


def test_rename_onto_protected_path_is_tier2(repo):
    r, _ = repo
    body = "def helper():\n    return 'x'\n" * 20  # enough for -M similarity
    write(r, "server/app/services/big.py", body)
    commit_all(r, "add big")
    git(r, "tag", "-f", "base")
    git(r, "mv", "server/app/services/big.py", "server/app/routers/auth.py")
    commit_all(r, "rename onto protected")
    result = run(repo)
    assert result["tier"] == 2
    assert "server/app/routers/auth.py" in result["protected_hits"]
    assert any("rename" in reason for reason in result["reasons"])


def test_rename_of_protected_file_away_is_tier2(repo):
    r, _ = repo
    git(r, "mv", "server/app/privacy.py", "server/app/services/harmless.py")
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert "server/app/privacy.py" in result["protected_hits"]


def test_new_file_in_migrations_is_tier2(repo):
    r, _ = repo
    write(r, "server/app/migrations/006_new.sql", "ALTER TABLE x ADD y INT;\n")
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert "server/app/migrations/006_new.sql" in result["protected_hits"]
    assert any("created at protected path" in reason for reason in result["reasons"])


# -- reachability promotion -------------------------------------------------


def test_new_import_from_protected_file_emits_promotion(repo):
    r, _ = repo
    write(r, "server/app/newmod.py", "def hook(msg):\n    return msg\n")
    write(
        r,
        "server/app/ws.py",
        "import json\nimport newmod\n\n\ndef fanout(msg):\n    return newmod.hook(msg)\n",
    )
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert "server/app/newmod.py" in result["proposed_promotions"]


def test_preexisting_imports_do_not_promote(repo):
    r, _ = repo
    write(r, "server/app/ws.py", "import json\n\n\ndef fanout(msg):\n    return json.dumps(msg)\n")
    commit_all(r)
    result = run(repo)
    assert result["proposed_promotions"] == []  # json import already existed


def test_ts_protected_file_new_relative_import_promotes(repo):
    r, _ = repo
    write(r, "client/src/helper.ts", "export const h = 2;\n")
    write(
        r,
        "client/src/protected/gate.ts",
        "import { api } from '../api';\nimport { h } from '../helper';\n\n"
        "export function gate() {\n  return api + h;\n}\n",
    )
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert "client/src/helper.ts" in result["proposed_promotions"]


# -- banned constructs ------------------------------------------------------


def test_importlib_introduced_in_protected_file_is_banned(repo):
    r, _ = repo
    write(
        r,
        "server/app/ws.py",
        "import importlib\n\n\ndef fanout(msg):\n"
        "    mod = importlib.import_module(msg['m'])\n    return mod\n",
    )
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert {"file": "server/app/ws.py", "construct": "importlib.import_module"} in result[
        "banned_constructs"
    ]


def test_computed_import_in_protected_ts_is_banned(repo):
    r, _ = repo
    write(
        r,
        "client/src/protected/gate.ts",
        "import { api } from '../api';\n\nexport async function gate(name: string) {\n"
        "  const mod = await import('../' + name);\n  return mod;\n}\n",
    )
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert {"file": "client/src/protected/gate.ts", "construct": "computed import()"} in result[
        "banned_constructs"
    ]


def test_literal_dynamic_import_in_protected_ts_is_not_banned(repo):
    r, _ = repo
    write(
        r,
        "client/src/protected/gate.ts",
        "import { api } from '../api';\n\nexport async function gate() {\n"
        "  const mod = await import('../api');\n  return mod;\n}\n",
    )
    commit_all(r)
    result = run(repo)
    assert result["banned_constructs"] == []


# -- gates ------------------------------------------------------------------


def test_failing_gates_never_tier0_or_tier1(repo):
    r, _ = repo
    write(r, "docs/notes.md", "inert change\n")
    commit_all(r)
    result = run(repo, gates=GATES_FAIL)
    assert result["tier"] == 2
    assert any("gate failed: tests" in reason for reason in result["reasons"])


def test_missing_gate_results_fail_closed(repo):
    r, _ = repo
    write(r, "docs/notes.md", "inert change\n")
    commit_all(r)
    result = run(repo, gates={})
    assert result["tier"] == 2


@pytest.mark.parametrize("gates", [
    {"tests": "false", "typecheck": "false", "build": "false"},  # F4: strings are truthy
    {"tests": "true", "typecheck": "true", "build": "true"},     # strings, not bools
    {"whatever": True},                                          # required gates absent
    {"tests": True},                                             # partial
    {"tests": True, "typecheck": True, "build": True, "x": True},  # extra key
    {"tests": 1, "typecheck": 1, "build": 1},                    # ints, not bools
])
def test_malformed_gates_fail_closed(repo, gates):
    """F4: only {tests,typecheck,build} all boolean-True passes; anything else is Tier 2."""
    r, _ = repo
    write(r, "docs/notes.md", "inert change\n")
    commit_all(r)
    result = run(repo, gates=gates)
    assert result["tier"] == 2, gates


# -- size cap ---------------------------------------------------------------


def test_oversized_diff_is_tier2(repo):
    r, _ = repo
    body = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(80))
    write(r, "server/app/services/util.py", body)
    commit_all(r)
    result = run(repo)
    assert result["stats"]["lines_added"] + result["stats"]["lines_removed"] > 150
    assert result["tier"] == 2
    assert any("size cap" in reason for reason in result["reasons"])


# -- staged mode + CLI ------------------------------------------------------


def test_staged_diff_touching_protected_is_tier2(repo):
    r, _ = repo
    write(r, "server/app/privacy.py", PRIVACY_PY + "\n# staged tweak\n")
    git(r, "add", "-A")
    result = run(repo, staged=True)
    assert result["tier"] == 2
    assert "server/app/privacy.py" in result["protected_hits"]


def test_cli_outputs_json(repo):
    r, cfg = repo
    write(r, "server/app/privacy.py", PRIVACY_PY + "\n# tweak\n")
    commit_all(r)
    script = Path(__file__).resolve().parents[1] / "classify_diff.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo", str(r),
            "--range", "base..HEAD",
            "--config", str(cfg),
            "--gates", json.dumps(GATES_PASS),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    assert out["tier"] == 2
    assert set(out) == {
        "tier", "reasons", "protected_hits", "proposed_promotions",
        "banned_constructs", "stats",
    }
