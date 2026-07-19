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


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    prot = raw.get("protected", {})
    inert = raw.get("inert", {})
    limits = raw.get("limits", {})
    return Config(
        protected_files={_norm(p) for p in prot.get("files", [])},
        protected_dirs=[_norm(p) for p in prot.get("dirs", [])],
        protected_patterns=list(prot.get("patterns", [])),
        inert_patterns=list(inert.get("patterns", [])),
        size_cap=int(limits.get("size_cap", 150)),
        daily_auto_apply_budget=int(limits.get("daily_auto_apply_budget", 12)),
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


def py_banned_labels(source: str) -> set[str]:
    """Banned dynamic-load constructs present in the source."""
    labels: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in ("exec", "eval", "__import__"):
                labels.add(node.id)
            elif node.id == "import_module":
                labels.add("importlib.import_module")
        elif isinstance(node, ast.Attribute):
            if node.attr == "import_module":
                labels.add("importlib.import_module")
            elif node.attr == "__import__":
                labels.add("__import__")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "importlib" and any(
                a.name == "import_module" for a in node.names
            ):
                labels.add("importlib.import_module")
    return labels


def _py_candidates(anchor: str, module: str) -> list[str]:
    modpath = module.replace(".", "/")
    stem = posixpath.normpath(posixpath.join(anchor, modpath)) if modpath else _norm(anchor or ".")
    return [f"{stem}.py", f"{stem}/__init__.py"]


def resolve_py_import(key: tuple, importer: str, tree: set[str]) -> str | None:
    """Resolve one import key to a repo-relative path in the new tree."""
    importer_dir = posixpath.dirname(importer)
    if key[0] == "abs":
        _, module, _ = key
        anchors = _ancestor_anchors(importer_dir)
        for anchor in anchors:
            for cand in _py_candidates(anchor, module):
                if cand in tree:
                    return cand
        return None
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
        stems = _py_candidates(anchor, module) if module else []
        # `from X import name` where name is itself a module
        modpath = posixpath.join(anchor, module.replace(".", "/")) if module else anchor
        modpath = _norm(modpath) if modpath else ""
        name_cand = _norm(posixpath.join(modpath, name)) if name != "*" else None
        for cand in stems + (
            [f"{name_cand}.py", f"{name_cand}/__init__.py"] if name_cand else []
        ):
            if cand in tree:
                return cand
    return None


def _ancestor_anchors(importer_dir: str) -> list[str]:
    """Importer's dir, each ancestor, then repo root — nearest first."""
    anchors = []
    d = importer_dir
    while d:
        anchors.append(d)
        d = posixpath.dirname(d)
    anchors.append("")
    return anchors


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


def resolve_ts_specifier(spec: str, importer: str, tree: set[str]) -> str | None:
    if not spec.startswith("."):
        return None  # bare specifier: package import, not a repo path
    stem = _norm(posixpath.join(posixpath.dirname(importer), spec))
    for suffix in _TS_RESOLVE_SUFFIXES:
        cand = stem + suffix
        if cand in tree:
            return cand
    return None


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

        if path.endswith(".py"):
            try:
                new_keys = py_import_keys(new_src)
                new_banned = py_banned_labels(new_src)
            except SyntaxError:
                reasons.append(f"unparseable protected python file: {path}")
                continue
            old_keys: set = set()
            old_banned: set = set()
            if old_src is not None:
                try:
                    old_keys = py_import_keys(old_src)
                    old_banned = py_banned_labels(old_src)
                except SyntaxError:
                    pass  # old unparseable: treat everything new as new
            for key in sorted(new_keys - old_keys):
                target = resolve_py_import(key, path, tree)
                if target and target != path and not is_protected(target, cfg):
                    promotions.add(target)
                    reasons.append(
                        f"reachability promotion: {path} newly imports {target}"
                    )
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
                target = resolve_ts_specifier(spec, path, tree)
                if target and target != path and not is_protected(target, cfg):
                    promotions.add(target)
                    reasons.append(
                        f"reachability promotion: {path} newly imports {target}"
                    )
            new_computed = ts_has_computed_import(new_src)
            old_computed = ts_has_computed_import(old_src) if old_src else False
            if new_computed and not old_computed:
                banned.append({"file": path, "construct": "computed import()"})
                reasons.append(
                    f"banned construct introduced in protected file: "
                    f"computed import() in {path}"
                )

    # -- gates
    failed_gates = [k for k, v in gates.items() if not v]
    gates_pass = bool(gates) and not failed_gates
    for g in failed_gates:
        reasons.append(f"gate failed: {g}")
    if not gates:
        reasons.append("no gate results provided (fail-closed)")

    # -- tier decision
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
