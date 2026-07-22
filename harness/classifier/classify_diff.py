#!/usr/bin/env python3
"""WP-H4: diff-tier classifier.

Pure function of (git diff, protected-paths config, gate results) -> tier.
The classifier never runs gates and never mutates anything; it reads the
repo via read-only git plumbing and emits a JSON verdict.

Tiers:
  0 - inert paths only, gates pass          -> auto-apply
  1 - small code diff, gates pass           -> auto-apply + post to #custodian
  2 - protected touch / promotion / banned  -> human gate
      construct / failed gate / oversized

Usage:
  classify_diff.py --repo . --range A..B --config protected-paths.toml \
      --gates '{"tests": true, "typecheck": true, "build": true}'
  classify_diff.py --repo . --staged --config protected-paths.toml --gates ...

Importable API: classify(repo, config, range_spec=..., staged=..., gates=...).
Stdlib only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import posixpath
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


@dataclass
class Config:
    protected_files: set[str] = field(default_factory=set)
    protected_dirs: list[str] = field(default_factory=list)
    protected_patterns: list[str] = field(default_factory=list)
    inert_patterns: list[str] = field(default_factory=list)
    size_cap: int = 150
    daily_auto_apply_budget: int = 12
    # H13-D1 stricter mode, default OFF (ships closed). When ON, a protected
    # file may not reference an unpromoted module at all: every promotion
    # target is ALSO emitted as a banned construct, so the diff is refused
    # rather than merely proposed for promotion. plink's lever, not a
    # resident's — it lives in the plink-owned protected-paths.toml.
    strict_reachability: bool = False


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    prot = raw.get("protected", {})
    inert = raw.get("inert", {})
    limits = raw.get("limits", {})
    modes = raw.get("modes", {})
    return Config(
        protected_files={_norm(p) for p in prot.get("files", [])},
        protected_dirs=[_norm(p) for p in prot.get("dirs", [])],
        protected_patterns=list(prot.get("patterns", [])),
        inert_patterns=list(inert.get("patterns", [])),
        size_cap=int(limits.get("size_cap", 150)),
        daily_auto_apply_budget=int(limits.get("daily_auto_apply_budget", 12)),
        # Only the literal boolean true enables it (a truthy string must not).
        strict_reachability=modes.get("strict_reachability", False) is True,
    )


def _norm(path: str) -> str:
    return posixpath.normpath(path.strip().lstrip("/"))


def _match_patterns(path: str, patterns: list[str]) -> bool:
    """fnmatch against the full repo-relative path AND the basename.

    fnmatch's '*' crosses '/' so '*.md' matches at any depth and
    'docs/*' matches recursively; basename matching lets '.env*'
    catch dotfiles in any directory.
    """
    base = posixpath.basename(path)
    return any(
        fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(base, pat)
        for pat in patterns
    )


def is_protected(path: str, cfg: Config) -> bool:
    path = _norm(path)
    if path in cfg.protected_files:
        return True
    for d in cfg.protected_dirs:
        if path == d or path.startswith(d + "/"):
            return True
    return _match_patterns(path, cfg.protected_patterns)


def is_inert(path: str, cfg: Config) -> bool:
    return _match_patterns(_norm(path), cfg.inert_patterns)


# --------------------------------------------------------------------------
# git plumbing (read-only)
# --------------------------------------------------------------------------


def _git(repo: str, *args: str, check: bool = True) -> str | None:
    r = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        if check:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {r.stderr.strip()}"
            )
        return None
    return r.stdout


@dataclass
class DiffEntry:
    status: str          # A, M, D, R, C, T
    old_path: str | None
    new_path: str | None


def _parse_range(repo: str, range_spec: str) -> tuple[str, str]:
    if "..." in range_spec:
        a, b = range_spec.split("...", 1)
        base = _git(repo, "merge-base", a, b).strip()
        return base, b
    if ".." in range_spec:
        a, b = range_spec.split("..", 1)
        return a, b
    raise ValueError(f"--range must be A..B or A...B, got {range_spec!r}")


def _diff_args(base: str | None, head: str | None, staged: bool) -> list[str]:
    if staged:
        return ["--cached"]
    return [base, head]


def diff_entries(repo, base, head, staged) -> list[DiffEntry]:
    out = _git(repo, "diff", "--name-status", "-M", *_diff_args(base, head, staged))
    entries = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]  # R100 -> R
        if status in ("R", "C") and len(parts) >= 3:
            entries.append(DiffEntry(status, _norm(parts[1]), _norm(parts[2])))
        elif status == "D":
            entries.append(DiffEntry(status, _norm(parts[1]), None))
        elif status == "A":
            entries.append(DiffEntry(status, None, _norm(parts[1])))
        else:  # M, T
            entries.append(DiffEntry(status, _norm(parts[1]), _norm(parts[1])))
    return entries


def diff_stats(repo, base, head, staged) -> dict:
    out = _git(repo, "diff", "--numstat", "-M", *_diff_args(base, head, staged))
    files = added = removed = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        if parts[0] != "-":
            added += int(parts[0])
        if parts[1] != "-":
            removed += int(parts[1])
    return {"files": files, "lines_added": added, "lines_removed": removed}


def old_blob(repo, base, path, staged) -> str | None:
    ref = "HEAD" if staged else base
    return _git(repo, "show", f"{ref}:{path}", check=False)


def new_blob(repo, head, path, staged) -> str | None:
    spec = f":0:{path}" if staged else f"{head}:{path}"
    return _git(repo, "show", spec, check=False)


def new_tree_paths(repo, head, staged) -> set[str]:
    if staged:
        out = _git(repo, "ls-files", "--cached")
    else:
        out = _git(repo, "ls-tree", "-r", "--name-only", head)
    return {_norm(p) for p in out.splitlines() if p.strip()}


# --------------------------------------------------------------------------
# Python analysis (ast)
# --------------------------------------------------------------------------


def py_import_keys(source: str) -> set[tuple]:
    """Canonical keys for every import statement, for old/new delta."""
    keys: set[tuple] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                keys.add(("abs", alias.name, None))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                keys.add(("from", node.level, node.module or "", alias.name))
    return keys


# --------------------------------------------------------------------------
# H13-D3: the dynamic-load ban
# --------------------------------------------------------------------------
#
# THREAT MODEL (WP-H13 red-team finding D3, and why this is deliberately
# over-broad):
#
#   The classifier is the gate that decides whether a resident-authored diff
#   can be auto-merged (Tier 1) or must be seen by a human (Tier 2). The whole
#   reachability-promotion machinery below is a STATIC import graph: it can
#   only see edges that are written as import statements. Any construct that
#   loads code by a name computed at runtime makes an edge the classifier
#   cannot see, so a protected file could reach unprotected code (and the
#   follow-up diff creating that code would look like an ordinary Tier 1
#   change). That is why dynamic loading is forbidden outright inside
#   protected files, not merely "reviewed".
#
#   The pre-H13 check was a literal-token check (`exec` / `eval` /
#   `__import__` / `import_module` as bare Name or Attribute). The red team
#   evaded it with, among others:
#       getattr(importlib, "import_module")("x")   # attribute name is data
#       builtins.exec(src)                         # different receiver
#       getattr(builtins, "__imp" "ort__")("x")    # token split across
#                                                  #   adjacent literals
#       globals()["ev" + "al"](src)                # namespace dict + concat
#       sys.modules["x"] = fake                    # module table mutation
#       types.ModuleType("x")                      # fabricate a module
#
# DESIGN RULE: this ban FAILS CLOSED and accepts false positives on purpose.
#   These constructs are forbidden in resident-authored protected code anyway,
#   so an over-broad ban costs a human an occasional "yes, that's fine, Tier 2
#   it" — while an under-broad ban costs an invisible reachability edge. When
#   in doubt, flag. Every entry below is a *label*; classify() only reports
#   labels the diff INTRODUCED (new set minus old set), so pre-existing code
#   does not re-flag on every touch.
#
# The check is AST-based (a token/regex scan cannot tell `re.compile` from
# `builtins.compile`, and cannot see implicit string concatenation, which the
# parser folds for us). If the file does not parse, we DO NOT pass it: we fall
# back to a squash-then-token scan AND emit `unparseable-python`, because an
# unparseable protected .py file is itself a red flag (it cannot be the tested,
# gate-passing artifact it claims to be).

# Modules whose mere presence in a protected file is banned: their entire
# surface is code loading. Touching the name at all (import, attribute base,
# bare reference) is enough.
_BAN_MODULE_ROOTS = {
    "importlib", "imp", "runpy", "zipimport", "pkgutil", "code", "codeop",
    "marshal", "ctypes", "builtins", "__builtin__",
}

# Modules that are fine in general but have specific loader-shaped attributes.
_BAN_MODULE_ATTRS = {
    # sys.modules / sys.path* rewrite what an import statement resolves to,
    # which silently invalidates every promotion this classifier computes.
    "sys": {
        "modules", "path", "meta_path", "path_hooks", "path_importer_cache",
        "settrace", "setprofile", "_getframe",
    },
    # types.ModuleType fabricates a module object out of thin air;
    # CodeType/FunctionType fabricate callables from raw code objects.
    "types": {"ModuleType", "CodeType", "FunctionType", "LambdaType"},
}

# Builtins that execute or import data.
_BAN_BUILTIN_NAMES = {
    "exec", "eval", "compile", "__import__", "execfile", "reload",
    "__builtins__", "__loader__", "__spec__",
}

# Builtins that hand out a namespace dict — the standard laundering step
# (`globals()["ex" + "ec"]`) that turns a string into a callable.
_BAN_REFLECT_NAMES = {"globals", "locals", "vars"}

# Attribute names banned regardless of receiver. A receiver can be aliased
# (`il = importlib; il.import_module(...)`) or arrive as a parameter, so the
# attribute name itself must be load-bearing.
_BAN_ATTR_NAMES = {
    "import_module", "__import__", "exec", "eval", "compile", "execfile",
    "reload", "exec_module", "load_module", "create_module",
    "module_from_spec", "spec_from_file_location", "spec_from_loader",
    "find_spec", "find_module", "get_loader", "run_path", "run_module",
    "ModuleType", "CodeType", "SourceFileLoader", "SourcelessFileLoader",
    "ExtensionFileLoader", "MetaPathFinder", "PathFinder",
    "InteractiveInterpreter", "InteractiveConsole", "compile_command",
    "load_source", "load_compiled", "resource_loader",
}

# The one carve-out: `re.compile` (and the drop-in `regex`) is not a code
# loader and is common in real protected code. Narrow, receiver-pinned, and
# deliberately NOT extended to aliases — `r = re; r.compile(...)` still flags,
# which is the fail-closed direction.
_ATTR_RECEIVER_EXEMPT = {"compile": {"re", "sre_compile", "regex"}}

# Tokens that must not appear as data. A string containing one of these is
# either building an import/exec call or documenting one; both go to a human.
_BAN_STRING_TOKENS = {
    "__import__", "import_module", "exec", "eval", "compile", "execfile",
    "importlib", "builtins", "__builtins__", "runpy", "imp", "zipimport",
    "ModuleType", "exec_module", "load_module", "module_from_spec",
    "spec_from_file_location", "InteractiveInterpreter", "run_path",
    "run_module", "reload",
}

# String constants longer than this are treated as prose, not as a token being
# assembled; keeps ordinary error messages/comments out of the ban.
_STRING_SCAN_MAX = 60
# A string is only read as a *name* (the thing getattr/import_module take)
# when it is a bare identifier or dotted path. Without this, prose like
# "exec-failure" or "cannot eval that" would flag; with it, "__import__",
# "exec" and "importlib.import_module" still do.
_NAME_LIKE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# Minimum length of a literal fragment considered a deliberate token split.
# 3 is required to catch the canonical `"imp" + "ort"` evasion.
_FRAGMENT_MIN = 3

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Squashes `"a" "b"`, `"a" + "b"`, `'a'\n  'b'` into `ab` so the textual
# fallback (unparseable files only) sees reconstructed tokens.
_SQUASH_CONCAT_RE = re.compile(r"""["']\s*\+?\s*["']""")


def _attr_root(node: ast.AST) -> str | None:
    """Leftmost Name of an attribute chain: importlib.util.find_spec -> 'importlib'."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _string_tokens(value: str) -> set[str]:
    return set(_IDENT_RE.findall(value))


def _literal_fragments(node: ast.AST) -> list[str]:
    """Literal str pieces of a runtime string-building expression.

    Covers `a + b`, f-strings, `"".join([...])`, `"{}".format(...)`,
    `s.replace(...)` and `%` formatting — the ways a banned token can be
    reassembled from pieces that are individually innocuous.
    """
    out: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        out += _literal_fragments(node.left)
        out += _literal_fragments(node.right)
    elif isinstance(node, ast.JoinedStr):
        for v in node.values:
            out += _literal_fragments(v)
    elif isinstance(node, ast.FormattedValue):
        out += _literal_fragments(node.value)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for e in node.elts:
            out += _literal_fragments(e)
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "join", "format", "replace", "removeprefix", "removesuffix",
            "lstrip", "rstrip", "strip",
        ):
            out += _literal_fragments(node.func.value)
            for a in node.args:
                out += _literal_fragments(a)
    return out


def _fragment_is_token_piece(frag: str) -> bool:
    """True if `frag` looks like a deliberate piece of a banned token."""
    if len(frag) < _FRAGMENT_MIN or not frag.replace("_", "").isalnum():
        return False
    return any(frag in tok for tok in _BAN_STRING_TOKENS)


def py_banned_labels(source: str) -> set[str]:
    """AST scan for banned dynamic-load constructs. Raises SyntaxError.

    Callers must treat a SyntaxError as suspicious, not as a pass — see
    py_scan_banned(), which is what classify() uses.
    """
    labels: set[str] = set()
    tree = ast.parse(source)

    # Nodes used as the callee of a call, and string constants that are bare
    # expression statements (docstrings / commented-out prose). Identity sets,
    # because ast nodes are not hashable-by-value.
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    doc_consts = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    # Names actually bound by an import statement in this file. The
    # module-root rules are gated on this because several banned module names
    # are ordinary English identifiers — `code` is the module, but also the
    # commonest name for an exit code or an invite code (server/cli.py has
    # `def _fail(msg, code=1)`), and `imp`/`marshal` are equally plausible
    # locals. Nothing is lost by the gate: obtaining one of these modules
    # WITHOUT an import statement requires getattr / sys.modules /
    # __import__, each of which is banned on its own, and loader-shaped
    # attribute names (_BAN_ATTR_NAMES) are still flagged on any receiver.
    bound_imports: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for alias in n.names:
                bound_imports.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for alias in n.names:
                bound_imports.add(alias.asname or alias.name)

    def flag_attr(name: str, root: str | None) -> None:
        if name not in _BAN_ATTR_NAMES:
            return
        if root in _ATTR_RECEIVER_EXEMPT.get(name, ()):
            return
        # Keep the historical label for the canonical construct so existing
        # audit records / tests stay meaningful.
        if name == "import_module":
            labels.add("importlib.import_module")
        elif name in ("exec", "eval", "compile", "__import__"):
            labels.add(name)
        else:
            labels.add(f"attr:{name}")

    for node in ast.walk(tree):
        # -- import statements naming the loader machinery ------------------
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BAN_MODULE_ROOTS:
                    labels.add(f"import-machinery:{root}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BAN_MODULE_ROOTS:
                labels.add(f"import-machinery:{root}")
            for alias in node.names:
                flag_attr(alias.name, None)
                if alias.name in _BAN_BUILTIN_NAMES:
                    labels.add(alias.name)
                if alias.name in _BAN_MODULE_ATTRS.get(root, ()):
                    labels.add(f"{root}.{alias.name}")
                if alias.name.split(".")[0] in _BAN_MODULE_ROOTS:
                    labels.add(f"import-machinery:{alias.name.split('.')[0]}")

        # -- attribute access ----------------------------------------------
        elif isinstance(node, ast.Attribute):
            root = _attr_root(node)
            if root in _BAN_MODULE_ROOTS and root in bound_imports:
                labels.add(f"import-machinery:{root}")
            if root in _BAN_MODULE_ATTRS and node.attr in _BAN_MODULE_ATTRS[root]:
                labels.add(f"{root}.{node.attr}")
            flag_attr(node.attr, root)

        # -- bare names ------------------------------------------------------
        elif isinstance(node, ast.Name):
            if node.id in _BAN_BUILTIN_NAMES:
                labels.add(node.id)
            elif node.id in _BAN_REFLECT_NAMES:
                labels.add(f"namespace-reflection:{node.id}")
            elif node.id == "import_module":
                labels.add("importlib.import_module")
            elif node.id in _BAN_MODULE_ROOTS and node.id in bound_imports:
                labels.add(f"import-machinery:{node.id}")
            elif node.id in ("getattr", "setattr", "delattr"):
                # Called forms are judged at the Call node (a literal, benign
                # attribute name is allowed there). A *reference* to getattr
                # that is not an immediate call is an alias — `g = getattr` —
                # and defeats that judgement, so it is banned outright.
                if id(node) not in call_funcs:
                    labels.add("dynamic-attribute-alias")

        # -- calls -----------------------------------------------------------
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in (
                "getattr", "setattr", "delattr"
            ):
                recv_root = _attr_root(node.args[0]) if node.args else None
                if recv_root in _BAN_MODULE_ROOTS or recv_root in _BAN_MODULE_ATTRS:
                    labels.add("getattr-on-import-machinery")
                name_arg = node.args[1] if len(node.args) > 1 else None
                if name_arg is None or not (
                    isinstance(name_arg, ast.Constant)
                    and isinstance(name_arg.value, str)
                ):
                    # The attribute name is computed: the classifier cannot
                    # know what is being reached. Fail closed.
                    labels.add("computed-attribute-access")
                else:
                    tokens = _string_tokens(name_arg.value)
                    for tok in sorted(tokens & _BAN_STRING_TOKENS):
                        labels.add(f"getattr-banned-name:{tok}")
            elif not isinstance(func, (ast.Name, ast.Attribute)):
                # `getattr(m, n)(...)`, `globals()["x"](...)`, `tbl[k](...)`:
                # the callee is not statically nameable. This is the generic
                # shape of "an attribute/lookup value flows to a call" that
                # D3 asks for, and it is what every evasion above ends in.
                labels.add("computed-callable-call")

        # -- string data -------------------------------------------------
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            if id(node) in doc_consts or len(value) > _STRING_SCAN_MAX:
                continue  # docstring / prose: not a token under construction
            if not _NAME_LIKE_RE.match(value):
                continue  # prose ("exec-failure", "cannot eval that"), not a name
            for tok in sorted(set(value.split(".")) & _BAN_STRING_TOKENS):
                labels.add(f"import-token-in-string:{tok}")

        # -- strings assembled at runtime ------------------------------------
        if isinstance(node, (ast.BinOp, ast.JoinedStr)) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("join", "format", "replace")
        ):
            frags = _literal_fragments(node)
            if len(frags) >= 2 or isinstance(node, ast.JoinedStr):
                joined = "".join(frags)
                if _string_tokens(joined) & _BAN_STRING_TOKENS or any(
                    _fragment_is_token_piece(f) for f in frags
                ):
                    labels.add("reconstructed-import-token")

    return labels


def py_banned_labels_textual(source: str) -> set[str]:
    """Token fallback for sources the AST cannot parse. Never a pass.

    Squashes adjacent/`+`-joined string literals first so `"__imp" "ort__"`
    is seen as `__import__`, then scans every identifier-shaped token against
    the union of the ban tables. Coarse by design: it only runs on files that
    are already being escalated for being unparseable.
    """
    banned = (
        _BAN_STRING_TOKENS
        | _BAN_BUILTIN_NAMES
        | _BAN_ATTR_NAMES
        | _BAN_MODULE_ROOTS
        | _BAN_REFLECT_NAMES
        | {"getattr", "setattr", "delattr"}
    )
    labels: set[str] = set()
    for text in (source, _SQUASH_CONCAT_RE.sub("", source)):
        for tok in set(_IDENT_RE.findall(text)):
            if tok in banned:
                labels.add(f"token:{tok}")
    return labels


def py_scan_banned(source: str) -> tuple[set[str], bool]:
    """(labels, parsed_ok). A file that does not parse fails CLOSED:
    it gets the textual scan plus an explicit `unparseable-python` label, so
    the diff carries a banned construct and can never be auto-merged."""
    try:
        return py_banned_labels(source), True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # ValueError: source with NUL bytes. RecursionError: deliberately
        # deep nesting. Both are hostile shapes, not honest code.
        return py_banned_labels_textual(source) | {"unparseable-python"}, False


# --------------------------------------------------------------------------
# H13-D1 / H13-D2: import resolution for reachability promotion
# --------------------------------------------------------------------------


def _py_candidates(anchor: str, module: str) -> list[str]:
    modpath = module.replace(".", "/")
    stem = posixpath.normpath(posixpath.join(anchor, modpath)) if modpath else _norm(anchor or ".")
    return [f"{stem}.py", f"{stem}/__init__.py"]


def _py_package_chain(anchor: str, module: str, tree: set[str]) -> list[str]:
    """`__init__.py` of every parent package along a dotted module path.

    `import a.b.c` executes a/__init__.py and a/b/__init__.py as well as
    a/b/c.py — all three are newly reachable and all three must be promoted.
    """
    parts = [p for p in module.split(".") if p]
    out = []
    for i in range(1, len(parts)):
        stem = posixpath.normpath(posixpath.join(anchor, "/".join(parts[:i])))
        cand = f"{stem}/__init__.py"
        if cand in tree:
            out.append(cand)
    return out


def _ancestor_anchors(importer_dir: str) -> list[str]:
    """Importer's dir, each ancestor, then repo root — nearest first."""
    anchors = []
    d = importer_dir
    while d:
        anchors.append(d)
        d = posixpath.dirname(d)
    anchors.append("")
    return anchors


def _prediction_anchors(anchors: list[str], module: str) -> list[str]:
    """Where an absent import target would plausibly be created.

    An unresolved import is either (a) a third-party/stdlib package — noise,
    but harmless noise a human declines — or (b) a wire to a file a LATER diff
    will create, which is exactly H13-D1. We cannot tell them apart, so we
    predict. To keep the report readable we only spray every anchor when the
    top-level name is NOT a stdlib module; for stdlib-shadowing names (`import
    code`, later `server/app/code.py`) the nearest anchor still gets a
    proposal, so the case is covered rather than silent.
    """
    top = module.split(".")[0] if module else ""
    if top and top in sys.stdlib_module_names:
        return anchors[:1]
    return anchors


def resolve_py_import_targets(
    key: tuple, importer: str, tree: set[str]
) -> tuple[list[str], list[str]]:
    """Resolve one import key to (present_targets, predicted_targets).

    present  — files that exist in the new tree and are newly reachable.
    predicted — files that do NOT exist yet but that this import wires the
                protected file to. H13-D1: emitting these is the whole point;
                without them a protected file can be wired to an absent module
                today and the module created as an ordinary unprotected file
                tomorrow.
    """
    importer_dir = posixpath.dirname(importer)
    present: list[str] = []
    predicted: list[str] = []

    if key[0] == "abs":
        _, module, _ = key
        anchors = _ancestor_anchors(importer_dir)
        for anchor in anchors:
            hits = [c for c in _py_candidates(anchor, module) if c in tree]
            if hits:
                present += hits
                present += _py_package_chain(anchor, module, tree)
                break
        else:
            for anchor in _prediction_anchors(anchors, module):
                predicted += _py_candidates(anchor, module)
        return present, predicted

    # ("from", level, module, name)
    _, level, module, name = key
    if level == 0:
        anchors = _ancestor_anchors(importer_dir)
    else:
        base = importer_dir
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        anchors = [base]

    for anchor in anchors:
        pkgdir = _norm(posixpath.join(anchor, module.replace(".", "/"))) if module else _norm(anchor or ".")
        pkgdir = "" if pkgdir == "." else pkgdir
        # H13-D2: the SUBMODULE is checked first. `from pkg import sub`
        # executes pkg/sub.py, and that is the file that must be protected;
        # the old resolver checked the package stem first and promoted only
        # pkg/__init__.py, leaving the reachable code file unprotected.
        name_cands = (
            [f"{_norm(posixpath.join(pkgdir, name))}.py",
             f"{_norm(posixpath.join(pkgdir, name))}/__init__.py"]
            if name != "*" else []
        )
        stem_cands = _py_candidates(anchor, module) if module else []
        sub_hits = [c for c in name_cands if c in tree]
        stem_hits = [c for c in stem_cands if c in tree]
        if not sub_hits and not stem_hits:
            continue
        # Ambiguous (`sub` is both a submodule and a name re-exported by the
        # package __init__): promote BOTH. Fail closed — over-promoting costs
        # a human a decline, under-promoting leaves live code unguarded.
        present += sub_hits + stem_hits
        present += _py_package_chain(anchor, module, tree)
        if not sub_hits and f"{pkgdir}/__init__.py" in stem_hits and name != "*":
            # H13-D1, package variant: `pkg` exists as a package but `sub`
            # does not exist as a submodule. `sub` may be an attribute of
            # __init__ (false positive, declined) or a submodule a later diff
            # will create (the attack). Predict it.
            predicted += name_cands
        break
    else:
        # Nothing about this import resolves anywhere: predict both the module
        # itself and, for `from mod import name`, the submodule under it.
        for anchor in _prediction_anchors(anchors, module):
            predicted += _py_candidates(anchor, module) if module else []
        near = anchors[0]
        pkgdir = _norm(posixpath.join(near, module.replace(".", "/"))) if module else _norm(near or ".")
        pkgdir = "" if pkgdir == "." else pkgdir
        if name != "*":
            stem = _norm(posixpath.join(pkgdir, name))
            predicted += [f"{stem}.py", f"{stem}/__init__.py"]

    return present, predicted


# --------------------------------------------------------------------------
# TypeScript analysis (regex; no TS compiler dependency)
# --------------------------------------------------------------------------

_TS_STATIC_IMPORT_RES = [
    # import defaultExport, { named } from 'spec'  /  import type { T } from 'spec'
    re.compile(r"""^\s*import\s+(?:type\s+)?[\w*{}\s,$]*?\s*from\s*['"]([^'"]+)['"]""", re.M),
    # side-effect import 'spec'
    re.compile(r"""^\s*import\s*['"]([^'"]+)['"]""", re.M),
    # export { x } from 'spec'  (re-export is an import edge too)
    re.compile(r"""^\s*export\s+(?:type\s+)?[\w*{}\s,$]*?\s*from\s*['"]([^'"]+)['"]""", re.M),
]

_TS_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*([^)\s][^)]*?)?\s*\)""")

_TS_RESOLVE_SUFFIXES = [
    "", ".ts", ".tsx", ".js", ".jsx",
    "/index.ts", "/index.tsx", "/index.js", "/index.jsx",
]


def ts_import_specifiers(source: str) -> set[str]:
    specs: set[str] = set()
    for rx in _TS_STATIC_IMPORT_RES:
        specs.update(rx.findall(source))
    return specs


def ts_has_computed_import(source: str) -> bool:
    """True if any import(...) call has a non-string-literal argument.

    Template literals (backticks) are treated as computed: even a constant
    template is an opaque load to this static check.
    """
    for m in _TS_DYNAMIC_IMPORT_RE.finditer(source):
        arg = (m.group(1) or "").strip()
        if not arg:
            continue  # `import()` alone: malformed, ignore
        if arg[0] in ("'", '"') and arg[-1] == arg[0]:
            continue  # literal specifier: allowed
        return True
    return False


# Suffixes proposed for a relative specifier that resolves to nothing today.
# Only the TS-authored forms: this repo's client is TypeScript, and a
# resident creating the absent target will create one of these.
_TS_PREDICT_SUFFIXES = [".ts", ".tsx", "/index.ts", "/index.tsx"]


def resolve_ts_specifier_targets(
    spec: str, importer: str, tree: set[str]
) -> tuple[list[str], list[str]]:
    """(present, predicted) for one specifier — TS mirror of the py resolver.

    H13-D1 applies identically here: `import { x } from './helper'` where
    helper.ts does not exist yet is a wire to a file a later diff creates.
    """
    if not spec.startswith("."):
        return [], []  # bare specifier: package import, not a repo path
    stem = _norm(posixpath.join(posixpath.dirname(importer), spec))
    hits = [stem + s for s in _TS_RESOLVE_SUFFIXES if stem + s in tree]
    if hits:
        return hits, []
    return [], [stem + s for s in _TS_PREDICT_SUFFIXES]


# --------------------------------------------------------------------------
# classifier core
# --------------------------------------------------------------------------


def classify(
    repo: str,
    config: Config | str,
    range_spec: str | None = None,
    staged: bool = False,
    gates: dict | None = None,
) -> dict:
    """Classify a diff. Pure function of (diff, config, gate results)."""
    cfg = load_config(config) if isinstance(config, str) else config
    if staged == bool(range_spec):
        raise ValueError("exactly one of range_spec / staged is required")
    base = head = None
    if range_spec:
        base, head = _parse_range(repo, range_spec)

    entries = diff_entries(repo, base, head, staged)
    stats = diff_stats(repo, base, head, staged)
    gates = gates or {}

    reasons: list[str] = []
    protected_hits: set[str] = set()
    promotions: set[str] = set()
    banned: list[dict] = []

    protected_entries: list[DiffEntry] = []
    unprotected_paths: list[str] = []

    for e in entries:
        sides = [p for p in (e.old_path, e.new_path) if p]
        hit = [p for p in sides if is_protected(p, cfg)]
        if hit:
            protected_hits.update(hit)
            protected_entries.append(e)
            if e.status in ("R", "C"):
                reasons.append(
                    f"rename/copy involves protected path: "
                    f"{e.old_path} -> {e.new_path}"
                )
            elif e.status == "A":
                reasons.append(f"file created at protected path: {e.new_path}")
            else:
                reasons.append(f"protected path touched: {sides[0]}")
        else:
            unprotected_paths.extend(sides)

    if protected_entries and unprotected_paths:
        reasons.append(
            "mixed diff (protected + unprotected changes): entire diff is Tier 2"
        )

    # -- deep analysis of changed protected files that still exist afterwards
    tree = None
    for e in protected_entries:
        path = e.new_path
        if path is None:  # deletion: nothing to parse on the new side
            continue
        if not (path.endswith(".py") or path.endswith((".ts", ".tsx"))):
            continue
        if tree is None:
            tree = new_tree_paths(repo, head, staged)
        new_src = new_blob(repo, head, path, staged)
        if new_src is None:
            continue
        old_src = old_blob(repo, base, e.old_path, staged) if e.old_path else None

        def record_promotions(wire: str, present: list[str], predicted: list[str]) -> None:
            """Emit promotion proposals for one new import edge.

            `present` targets exist today; `predicted` ones do NOT (H13-D1) —
            they are proposed anyway so that the follow-up diff which creates
            the file cannot land as a fresh, unprotected, auto-mergeable file.
            """
            for target in dict.fromkeys(present):
                if target != path and not is_protected(target, cfg):
                    promotions.add(target)
                    reasons.append(
                        f"reachability promotion: {path} newly imports {target}"
                    )
            absent = [
                t for t in dict.fromkeys(predicted)
                if t != path and not is_protected(t, cfg)
            ]
            if absent:
                promotions.update(absent)
                reasons.append(
                    f"reachability promotion (ABSENT TARGET, H13-D1): {path} "
                    f"newly wires to '{wire}', which resolves to nothing in "
                    f"this tree; proposing predicted path(s) "
                    f"{', '.join(absent)} so a later diff cannot create the "
                    f"target as an unprotected file (decline if '{wire}' is a "
                    f"third-party/stdlib package)"
                )
            if cfg.strict_reachability:
                # Stricter mode (default OFF): a protected file may not
                # reference an unpromoted module at all — the reference itself
                # is refused rather than merely proposed for promotion.
                for target in sorted(set(present) | set(predicted)):
                    if target != path and not is_protected(target, cfg):
                        banned.append({
                            "file": path,
                            "construct": f"unpromoted-reference:{target}",
                        })

        if path.endswith(".py"):
            # Banned-construct scan first: it must run even when the file does
            # not parse (fail closed — see py_scan_banned).
            new_banned, new_ok = py_scan_banned(new_src)
            new_keys: set = set()
            if new_ok:
                new_keys = py_import_keys(new_src)
            else:
                reasons.append(
                    f"unparseable protected python file (fail-closed: treated "
                    f"as a banned construct, import graph unknown): {path}"
                )
            old_keys: set = set()
            old_banned: set = set()
            if old_src is not None:
                old_banned, old_ok = py_scan_banned(old_src)
                if old_ok:
                    old_keys = py_import_keys(old_src)
                else:
                    # Old side unparseable: nothing about it can be trusted as
                    # a baseline, so every construct on the new side counts as
                    # newly introduced.
                    old_banned = set()
            for key in sorted(new_keys - old_keys):
                present, predicted = resolve_py_import_targets(key, path, tree)
                wire = key[1] if key[0] == "abs" else (
                    "." * key[1] + (key[2] or "") + f" import {key[3]}"
                )
                record_promotions(wire, present, predicted)
            for label in sorted(new_banned - old_banned):
                banned.append({"file": path, "construct": label})
                reasons.append(
                    f"banned construct introduced in protected file: "
                    f"{label} in {path}"
                )
        else:  # .ts / .tsx
            new_specs = ts_import_specifiers(new_src)
            old_specs = ts_import_specifiers(old_src) if old_src else set()
            for spec in sorted(new_specs - old_specs):
                present, predicted = resolve_ts_specifier_targets(spec, path, tree)
                record_promotions(spec, present, predicted)
            new_computed = ts_has_computed_import(new_src)
            old_computed = ts_has_computed_import(old_src) if old_src else False
            if new_computed and not old_computed:
                banned.append({"file": path, "construct": "computed import()"})
                reasons.append(
                    f"banned construct introduced in protected file: "
                    f"computed import() in {path}"
                )

    # -- gates
    # Gate results must be the exact shape {tests,typecheck,build: bool}, all
    # true, to pass. Fail-closed on anything else — a missing required gate, a
    # non-bool value (the string "false" is truthy!), or an unexpected key.
    # (WP-H13 F4: the old check only fail-closed on empty {}, so a malformed
    # or partial gates object silently passed and dropped a diff to Tier 1.)
    REQUIRED_GATES = ("tests", "typecheck", "build")
    bad_gate_shape = (
        set(gates) != set(REQUIRED_GATES)
        or any(v is not True for v in gates.values())
    )
    failed_gates = [k for k, v in gates.items() if v is not True]
    gates_pass = bool(gates) and not bad_gate_shape
    for g in failed_gates:
        reasons.append(f"gate failed: {g}")
    if not gates:
        reasons.append("no gate results provided (fail-closed)")
    elif bad_gate_shape and not failed_gates:
        reasons.append(f"gate results not the required shape {list(REQUIRED_GATES)} "
                       "of booleans (fail-closed)")

    # -- tier decision
    # PRECEDENCE (load-bearing, do not reorder): protection is decided BEFORE
    # the inert allowlist. A path can match both — a resident's spine entry is
    # markdown, and `*.md` is on the Tier-0 allowlist — and in that case it
    # must be Tier 2. Inert is an allowlist of last resort, never an override.
    # Pinned by tests/test_resident_paths.py.
    if protected_hits or promotions or banned or not gates_pass:
        tier = 2
    else:
        all_paths = [p for e in entries for p in (e.old_path, e.new_path) if p]
        total_lines = stats["lines_added"] + stats["lines_removed"]
        if all(is_inert(p, cfg) for p in all_paths):
            tier = 0
            reasons.append("inert paths only, gates pass")
        elif total_lines <= cfg.size_cap:
            tier = 1
            reasons.append(
                f"code diff within size cap "
                f"({total_lines} <= {cfg.size_cap}), gates pass"
            )
        else:
            tier = 2
            reasons.append(
                f"diff exceeds size cap ({total_lines} > {cfg.size_cap})"
            )

    return {
        "tier": tier,
        "reasons": reasons,
        "protected_hits": sorted(protected_hits),
        "proposed_promotions": sorted(promotions),
        "banned_constructs": banned,
        "stats": stats,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Disjorn diff-tier classifier (WP-H4)")
    p.add_argument("--repo", default=".", help="path to the git repo")
    spec = p.add_mutually_exclusive_group(required=True)
    spec.add_argument("--range", dest="range_spec", help="diff range A..B")
    spec.add_argument("--staged", action="store_true", help="classify the staged diff")
    p.add_argument("--config", required=True, help="path to protected-paths.toml")
    p.add_argument(
        "--gates",
        default="{}",
        help='gate results as JSON, e.g. \'{"tests": true, "typecheck": true, "build": true}\'',
    )
    args = p.parse_args(argv)
    try:
        gates = json.loads(args.gates)
        if not isinstance(gates, dict):
            raise ValueError("--gates must be a JSON object")
        result = classify(
            args.repo,
            args.config,
            range_spec=args.range_spec,
            staged=args.staged,
            gates=gates,
        )
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
