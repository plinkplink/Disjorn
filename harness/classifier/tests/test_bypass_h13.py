"""WP-H13 red-team regression tests — findings D1, D2, D3.

Every test in this file is an ATTACK the pre-fix classifier let through (or,
where marked "guard", a false-positive/ship-closed check that pins the new
behaviour). They are written against the same fixture repo as
test_classify_diff.py: `server/app/ws.py` is a protected file, everything
under `server/app/services/` is not.

The shape of all three findings is the same two-step vector:
    step 1 — a diff touching a PROTECTED file widens reachability in a way the
             classifier cannot see, so it emits no promotion proposal and no
             banned construct (the diff is still Tier 2 for the protected
             touch, but its machine-checkable artifact is empty/wrong);
    step 2 — a follow-up diff creates the newly-reachable file as an ordinary
             unprotected file, which is Tier 1 and (once merge-tier1 exists)
             auto-merges with no human in the loop.
Closing step 1's blind spots is what these tests lock down.
"""
import pytest

from classify_diff import classify, py_scan_banned

# Fixtures/helpers are shared with the WP-H4 suite; importing the fixture name
# into this module's namespace is how pytest makes it available here.
from test_classify_diff import (  # noqa: F401  (repo is a fixture)
    CONFIG,
    GATES_PASS,
    commit_all,
    git,
    repo,
    run,
    write,
)

WS_HEAD = "import json\n\n\ndef fanout(msg):\n    return json.dumps(msg)\n"


def banned_labels(result):
    return {b["construct"] for b in result["banned_constructs"]}


def wire_ws(r, body):
    """Rewrite the protected file server/app/ws.py with `body` and commit."""
    write(r, "server/app/ws.py", body)
    commit_all(r)


# ==========================================================================
# H13-D3 — the dynamic-import ban is bypassable
# ==========================================================================
#
# The pre-fix ban was a literal-token check: bare Name / Attribute `exec`,
# `eval`, `__import__`, `import_module`. Each case below reaches the import
# machinery without ever writing one of those four tokens where the old check
# looked. All of them must now land in `banned_constructs` — the field
# MERGE-CONTRACT step 3 keys `merge-refused` off.


@pytest.mark.parametrize("label,body", [
    # ---- named in the finding -------------------------------------------
    (
        "getattr-on-importlib",
        "import importlib\n\n\ndef fanout(msg):\n"
        "    return getattr(importlib, 'import_module')(msg['m'])\n",
    ),
    (
        "builtins.exec",
        "import builtins\n\n\ndef fanout(msg):\n"
        "    builtins.exec(msg['src'])\n    return msg\n",
    ),
    (
        "split-token-implicit-concat",
        "def fanout(msg):\n"
        "    f = getattr(__builtins__, '__imp' 'ort__')\n"
        "    return f(msg['m'])\n",
    ),
    (
        "split-token-plus-concat",
        "def fanout(msg, b):\n"
        "    return getattr(b, '__imp' + 'ort__')(msg['m'])\n",
    ),
    (
        "eval-via-globals-dict",
        "def fanout(msg):\n"
        "    return globals()['ev' + 'al'](msg['src'])\n",
    ),
    (
        "sys.modules-mutation",
        "import sys\n\n\ndef fanout(msg):\n"
        "    sys.modules['server.app.shim'] = msg['mod']\n    return msg\n",
    ),
    # ---- the same family, other doors ------------------------------------
    (
        "types.ModuleType",
        "import types\n\n\ndef fanout(msg):\n"
        "    m = types.ModuleType('shim')\n    return m\n",
    ),
    (
        "runpy",
        "import runpy\n\n\ndef fanout(msg):\n"
        "    return runpy.run_path(msg['p'])\n",
    ),
    (
        "imp-legacy",
        "import imp\n\n\ndef fanout(msg):\n"
        "    return imp.load_source('x', msg['p'])\n",
    ),
    (
        "code-InteractiveInterpreter",
        "import code\n\n\ndef fanout(msg):\n"
        "    return code.InteractiveInterpreter().runsource(msg['s'])\n",
    ),
    (
        "compile-builtin",
        "def fanout(msg):\n"
        "    c = compile(msg['src'], '<msg>', 'exec')\n    return c\n",
    ),
    (
        "importlib-spec-loader",
        "from importlib.util import spec_from_file_location\n\n\n"
        "def fanout(msg):\n    return spec_from_file_location('x', msg['p'])\n",
    ),
    (
        "computed-attribute-access",
        "def fanout(msg, obj):\n"
        "    return getattr(obj, msg['attr'])(msg['arg'])\n",
    ),
    (
        "getattr-alias",
        "g = getattr\n\n\ndef fanout(msg, obj):\n"
        "    return g(obj, 'x')\n",
    ),
    (
        "sys.path-mutation",
        "import sys\n\n\ndef fanout(msg):\n"
        "    sys.path.insert(0, msg['dir'])\n    return msg\n",
    ),
])
def test_dynamic_load_evasion_is_banned(repo, label, body):
    """D3: every named evasion must produce a banned construct, not a pass."""
    r, _ = repo
    wire_ws(r, body)
    result = run(repo)
    assert result["tier"] == 2
    assert result["banned_constructs"], f"{label} produced no banned construct"
    assert all(b["file"] == "server/app/ws.py" for b in result["banned_constructs"])


def test_unparseable_protected_python_fails_closed(repo):
    """D3 fail-closed: a protected .py that does not parse is NOT a pass.

    The old code appended a reason and `continue`d — no banned construct, so
    the machine-readable artifact said "nothing wrong here". A file that does
    not parse cannot be the tested artifact the gates signed off on, and its
    import graph is unknown; it must carry a banned construct.
    """
    r, _ = repo
    wire_ws(r, "def fanout(:\n    __import__('os')\n")
    result = run(repo)
    assert result["tier"] == 2
    assert "unparseable-python" in banned_labels(result)
    # the textual fallback still reports what it can see
    assert "token:__import__" in banned_labels(result)


def test_bare_eval_still_banned(repo):
    """Guard: the pre-H13 literal check must not have been loosened."""
    r, _ = repo
    wire_ws(r, "def fanout(msg):\n    return eval(msg['src'])\n")
    assert "eval" in banned_labels(run(repo))


def test_bare_exec_and_dunder_import_still_banned(repo):
    """Guard: same, for the other two original tokens."""
    r, _ = repo
    wire_ws(r, "def fanout(msg):\n    exec(msg['src'])\n    return __import__('os')\n")
    labels = banned_labels(run(repo))
    assert {"exec", "__import__"} <= labels


# -- false-positive cost: what must NOT be banned --------------------------


def test_re_compile_is_not_banned(repo):
    """Guard: `compile` is banned as a builtin/attribute, but re.compile is
    the one receiver-pinned carve-out — protected files do use regexes."""
    r, _ = repo
    wire_ws(
        r,
        "import re\n\nRX = re.compile(r'^[a-z]+$')\n\n\n"
        "def fanout(msg):\n    return RX.match(msg['t'])\n",
    )
    assert banned_labels(run(repo)) == set()


def test_ordinary_protected_edit_has_no_banned_constructs(repo):
    """Guard: the widened ban must not make routine edits to protected files
    unclassifiable — literal getattr, f-strings and method calls stay clean."""
    r, _ = repo
    wire_ws(
        r,
        "import json\n\n\ndef fanout(msg, actor):\n"
        "    who = getattr(actor, 'name', 'anon')\n"
        "    return json.dumps({'m': msg, 'who': f'user {who}'})\n",
    )
    assert banned_labels(run(repo)) == set()


def test_docstring_mentioning_exec_is_not_banned(repo):
    """Guard: prose about a banned token is prose. Docstrings and long
    strings are exempt from the string-token scan."""
    r, _ = repo
    wire_ws(
        r,
        '"""Fanout. Never call exec or eval on message content."""\n'
        "import json\n\n\ndef fanout(msg):\n"
        '    """Return the message; we do not eval anything here."""\n'
        "    return json.dumps(msg)\n",
    )
    assert banned_labels(run(repo)) == set()


def test_identifier_shadowing_a_banned_module_is_not_banned(repo):
    """Guard: `code`, `imp` and friends are banned MODULE names, but also
    ordinary local names — server/cli.py already has `def _fail(msg, code=1)`.
    The module-root rules are gated on the name actually being bound by an
    import; without that gate this file would flag on the word 'code'."""
    r, _ = repo
    wire_ws(
        r,
        "import json\n\n\ndef fanout(msg, code=1):\n"
        "    imp = {'code': code}\n"
        "    return json.dumps(imp)\n",
    )
    assert banned_labels(run(repo)) == set()


def test_imported_module_named_code_is_still_banned(repo):
    """...and the gate does not open a door: once `code` is imported it is
    the module again, and both the import and the loader attribute flag."""
    r, _ = repo
    wire_ws(
        r,
        "from code import InteractiveInterpreter\n\n\ndef fanout(msg):\n"
        "    return InteractiveInterpreter().runsource(msg['s'])\n",
    )
    labels = banned_labels(run(repo))
    assert "import-machinery:code" in labels
    assert "attr:InteractiveInterpreter" in labels


def test_prose_string_containing_a_banned_word_is_not_banned(repo):
    """Guard: banned tokens are banned as NAMES (what getattr/import_module
    take), not as English. 'exec-failure' is a broker error code, not an
    attribute name."""
    r, _ = repo
    wire_ws(
        r,
        "import json\n\nERR = 'exec-failure'\n\n\ndef fanout(msg):\n"
        "    if not msg:\n"
        "        return json.dumps({'e': ERR, 'why': 'cannot eval that input'})\n"
        "    return json.dumps(msg)\n",
    )
    assert banned_labels(run(repo)) == set()


def test_name_like_string_containing_a_banned_token_is_banned(repo):
    """...but a bare-identifier / dotted-path string still is — that is the
    shape every getattr-based evasion needs."""
    r, _ = repo
    wire_ws(
        r,
        "import json\n\nHOOK = 'importlib.import_module'\n\n\n"
        "def fanout(msg):\n    return json.dumps({'h': HOOK})\n",
    )
    assert "import-token-in-string:import_module" in banned_labels(run(repo))


def test_unprotected_file_may_still_use_dynamic_imports(repo):
    """Guard: the ban applies to PROTECTED files only. Ordinary code is not
    the authorization surface and is not policed by this rule."""
    r, _ = repo
    write(
        r,
        "server/app/services/util.py",
        "import importlib\n\n\ndef util(name):\n"
        "    return importlib.import_module(name)\n",
    )
    commit_all(r)
    result = run(repo)
    assert result["banned_constructs"] == []
    assert result["tier"] == 1


# -- unit-level: the scanner itself ----------------------------------------


def test_py_scan_banned_reports_parse_failure(repo):
    labels, ok = py_scan_banned("def f(:\n")
    assert ok is False
    assert "unparseable-python" in labels


def test_py_scan_banned_rejects_nul_bytes():
    """A NUL byte makes ast.parse raise ValueError, not SyntaxError; that
    path must fail closed too."""
    labels, ok = py_scan_banned("x = 1\n\x00\n")
    assert ok is False
    assert "unparseable-python" in labels


# ==========================================================================
# H13-D1 — reachability under-detection (absent / not-yet-created targets)
# ==========================================================================


def test_absent_import_target_emits_promotion(repo):
    """D1: wiring a protected file to a module that does not exist yet must
    still propose the promotion. Pre-fix this emitted NOTHING, which is what
    let step 2 create the target as a fresh unprotected file."""
    r, _ = repo
    wire_ws(
        r,
        "import json\nimport ghostmod\n\n\ndef fanout(msg):\n"
        "    return ghostmod.hook(json.dumps(msg))\n",
    )
    result = run(repo)
    assert result["tier"] == 2
    assert "server/app/ghostmod.py" in result["proposed_promotions"]
    assert any("ABSENT TARGET" in reason for reason in result["reasons"])


def test_two_step_absent_target_attack_is_visible_at_step_one(repo):
    """D1 end to end: the path proposed by the step-1 wire is exactly the path
    step 2 creates, so adding the proposal to protected-paths.toml (the human
    action a promotion proposal asks for) makes step 2 a protected touch."""
    r, cfg = repo
    wire_ws(
        r,
        "import json\nimport ghostmod\n\n\ndef fanout(msg):\n"
        "    return ghostmod.hook(json.dumps(msg))\n",
    )
    step1 = run(repo)
    assert "server/app/ghostmod.py" in step1["proposed_promotions"]

    # step 2: create the target. Without the step-1 proposal this is a small,
    # gate-passing, unprotected diff — Tier 1, auto-mergeable.
    git(r, "tag", "-f", "base")
    write(r, "server/app/ghostmod.py", "def hook(msg):\n    return msg\n")
    commit_all(r, "create the wired target")
    step2 = run(repo)
    assert step2["tier"] == 1  # ...which is exactly why step 1 must speak up

    # with the proposal accepted, step 2 is a protected touch
    cfg2 = cfg.parent / "protected-with-promotion.toml"
    cfg2.write_text(CONFIG.replace(
        '"server/app/ws.py",', '"server/app/ws.py",\n    "server/app/ghostmod.py",'
    ))
    step2_promoted = classify(str(r), str(cfg2), range_spec="base..HEAD", gates=GATES_PASS)
    assert step2_promoted["tier"] == 2
    assert "server/app/ghostmod.py" in step2_promoted["protected_hits"]


def test_absent_submodule_of_existing_package_emits_promotion(repo):
    """D1 (package variant): `pkg` exists, `pkg.sub` does not — yet."""
    r, _ = repo
    write(r, "server/app/pkg/__init__.py", "")
    commit_all(r, "add package")
    git(r, "tag", "-f", "base")
    wire_ws(
        r,
        "import json\nfrom pkg import ghost\n\n\ndef fanout(msg):\n"
        "    return ghost.hook(json.dumps(msg))\n",
    )
    result = run(repo)
    assert "server/app/pkg/ghost.py" in result["proposed_promotions"]


def test_absent_relative_import_target_emits_promotion(repo):
    """D1: relative imports are unambiguously repo-internal, so an absent
    relative target is predicted at exactly one place."""
    r, _ = repo
    wire_ws(
        r,
        "import json\nfrom . import ghostsib\n\n\ndef fanout(msg):\n"
        "    return ghostsib.hook(json.dumps(msg))\n",
    )
    result = run(repo)
    assert "server/app/ghostsib.py" in result["proposed_promotions"]


def test_absent_ts_relative_import_emits_promotion(repo):
    """D1, TypeScript side: same wire, same blind spot, same fix."""
    r, _ = repo
    write(
        r,
        "client/src/protected/gate.ts",
        "import { api } from '../api';\nimport { g } from '../ghost';\n\n"
        "export function gate() {\n  return api + g;\n}\n",
    )
    commit_all(r)
    result = run(repo)
    assert result["tier"] == 2
    assert "client/src/ghost.ts" in result["proposed_promotions"]
    assert "client/src/ghost.tsx" in result["proposed_promotions"]


def test_absent_target_promotion_skips_already_protected_paths(repo):
    """Guard: a predicted path that is already protected is not re-proposed —
    promotions are the *widening* set, not a dump of every edge."""
    r, _ = repo
    wire_ws(
        r,
        "import json\nimport privacy\n\n\ndef fanout(msg):\n"
        "    return privacy.check(json.dumps(msg))\n",
    )
    result = run(repo)
    assert "server/app/privacy.py" not in result["proposed_promotions"]


def test_stdlib_named_import_predicts_only_the_nearest_anchor(repo):
    """FP control: an unresolved stdlib-named import still gets a proposal
    (it could be a future shadowing module) but does not spray every anchor."""
    r, _ = repo
    wire_ws(r, "import json\nimport secrets\n\n\ndef fanout(msg):\n    return msg\n")
    proms = run(repo)["proposed_promotions"]
    assert "server/app/secrets.py" in proms
    assert "secrets.py" not in proms
    assert "server/secrets.py" not in proms


# -- stricter mode (H13-D1 second option) — ships OFF ----------------------


def strict_cfg(cfg):
    p = cfg.parent / "protected-paths-strict.toml"
    p.write_text(CONFIG + "\n[modes]\nstrict_reachability = true\n")
    return p


def test_strict_reachability_is_off_by_default(repo):
    """Ship-closed: the shipped config must not enable the stricter mode."""
    from classify_diff import load_config

    _, cfg = repo
    assert load_config(str(cfg)).strict_reachability is False
    shipped = str(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "protected-paths.toml"
    )
    assert load_config(shipped).strict_reachability is False


def test_strict_reachability_mode_refuses_the_reference(repo):
    """With the mode ON, a protected file may not reference an unpromoted
    module at all: the reference is a banned construct, not just a proposal."""
    r, cfg = repo
    write(r, "server/app/newmod.py", "def hook(msg):\n    return msg\n")
    wire_ws(
        r,
        "import json\nimport newmod\n\n\ndef fanout(msg):\n"
        "    return newmod.hook(json.dumps(msg))\n",
    )
    lax = run(repo)
    assert lax["banned_constructs"] == []  # default mode: proposal only
    assert "server/app/newmod.py" in lax["proposed_promotions"]

    strict = classify(
        str(r), str(strict_cfg(cfg)), range_spec="base..HEAD", gates=GATES_PASS
    )
    assert "unpromoted-reference:server/app/newmod.py" in banned_labels(strict)


# ==========================================================================
# H13-D2 — `from pkg import sub` promoted __init__.py, not the submodule
# ==========================================================================


def test_from_pkg_import_sub_promotes_the_submodule(repo):
    """D2: the file that actually runs is pkg/sub.py. The old resolver checked
    the package stem first, returned pkg/__init__.py and stopped — leaving the
    reachable code file unprotected and auto-mergeable."""
    r, _ = repo
    write(r, "server/app/pkg/__init__.py", "")
    write(r, "server/app/pkg/sub.py", "def hook(msg):\n    return msg\n")
    commit_all(r, "add package with submodule")
    git(r, "tag", "-f", "base")
    wire_ws(
        r,
        "import json\nfrom pkg import sub\n\n\ndef fanout(msg):\n"
        "    return sub.hook(json.dumps(msg))\n",
    )
    result = run(repo)
    assert result["tier"] == 2
    assert "server/app/pkg/sub.py" in result["proposed_promotions"]
    # ambiguous by construction (`sub` could also be a name re-exported by
    # __init__): fail closed and promote BOTH.
    assert "server/app/pkg/__init__.py" in result["proposed_promotions"]


def test_dotted_import_promotes_the_whole_package_chain(repo):
    """`import a.b.c` executes a/__init__.py and a/b/__init__.py too; all of
    them are newly reachable from the protected file."""
    r, _ = repo
    write(r, "server/app/pkg/__init__.py", "")
    write(r, "server/app/pkg/deep/__init__.py", "")
    write(r, "server/app/pkg/deep/leaf.py", "def hook(m):\n    return m\n")
    commit_all(r, "add nested package")
    git(r, "tag", "-f", "base")
    wire_ws(
        r,
        "import json\nimport pkg.deep.leaf\n\n\ndef fanout(msg):\n"
        "    return pkg.deep.leaf.hook(json.dumps(msg))\n",
    )
    proms = run(repo)["proposed_promotions"]
    assert "server/app/pkg/deep/leaf.py" in proms
    assert "server/app/pkg/__init__.py" in proms
    assert "server/app/pkg/deep/__init__.py" in proms


def test_from_pkg_import_function_promotes_only_the_package(repo):
    """Guard: when `sub` is plainly a function of a MODULE (not a package),
    no bogus submodule file is invented — `from mod import f` cannot be a
    submodule import in Python."""
    r, _ = repo
    wire_ws(
        r,
        "import json\nfrom services.util import util\n\n\ndef fanout(msg):\n"
        "    return util()\n",
    )
    proms = run(repo)["proposed_promotions"]
    assert "server/app/services/util.py" in proms
    assert "server/app/services/util/util.py" not in proms
