# harness/classifier — diff-tier classifier (WP-H4)

Pure function of **(git diff, `protected-paths.toml`, gate results) → tier**.
It never runs tests/typecheck/build itself — gate results are passed *in* —
and it never mutates anything: read-only git plumbing, JSON verdict out.

## Usage

```sh
# classify a commit range
python harness/classifier/classify_diff.py \
    --repo . --range main..feature \
    --config harness/classifier/protected-paths.toml \
    --gates '{"tests": true, "typecheck": true, "build": true}'

# classify the staged diff (index vs HEAD)
python harness/classifier/classify_diff.py \
    --repo . --staged --config harness/classifier/protected-paths.toml \
    --gates '{"tests": true, "typecheck": true, "build": true}'
```

Importable API: `from classify_diff import classify` —
`classify(repo, config, range_spec="A..B" | staged=True, gates={...}) -> dict`.
`config` may be a path or a preloaded `Config`. `A...B` (merge-base) ranges
are also accepted. Stdlib only; Python 3.11+.

Output (stdout):

```json
{
  "tier": 0 | 1 | 2,
  "reasons": ["human-readable rule hits"],
  "protected_hits": ["repo-relative protected paths touched"],
  "proposed_promotions": ["paths newly reachable from protected files"],
  "banned_constructs": [{"file": "...", "construct": "..."}],
  "stats": {"files": n, "lines_added": n, "lines_removed": n}
}
```

## Rule reference (HARNESS-PLAN.md WP-H4)

1. **Enumerated protection**: any touch of a `[protected]` file, anything
   under a `[protected]` dir, or a `patterns` match (e.g. `.env*`) → Tier 2.
2. **No smuggling**: a mixed diff (protected + unprotected hunks) is
   *entirely* Tier 2.
3. **Renames/moves**: protected if *either* side is protected. File
   *creation* inside a protected dir counts as protected.
4. **Reachability promotion**: for each changed protected `.py` file, old
   and new versions are AST-parsed and the import-set delta computed; new
   imports resolving to repo-relative, previously-unprotected paths are
   emitted in `proposed_promotions`. Changed protected `.ts`/`.tsx` files
   get the same treatment via a static `import ... from '...'` /
   `export ... from '...'` specifier delta (regex — no TS compiler);
   relative specifiers are resolved to repo paths. Since WP-H13:
   - **absent targets are promoted too** (D1). A new import that resolves to
     *nothing* in the tree still emits a proposal for the path(s) the target
     would occupy, so a follow-up diff cannot create it as a fresh
     unprotected (auto-mergeable) file. Reason lines say `ABSENT TARGET`.
     Third-party/stdlib imports therefore produce declinable false
     positives — that is the intended trade.
   - **`from pkg import sub` promotes the submodule** (D2), not just
     `pkg/__init__.py`; when both could be meant, BOTH are promoted, and
     `import a.b.c` also promotes the `__init__.py` chain it executes.
5. **Dynamic-load ban** (widened in WP-H13, finding D3): a diff *introducing*
   any dynamic code-loading construct into a protected Python file is flagged
   in `banned_constructs` → Tier 2. AST-based and deliberately over-broad:
   `exec`/`eval`/`compile`/`__import__`, `importlib`/`imp`/`runpy`/`pkgutil`/
   `code`/`marshal`/`ctypes`/`builtins` (any use), `sys.modules`/`sys.path`
   mutation, `types.ModuleType`, loader attributes (`exec_module`,
   `module_from_spec`, `spec_from_file_location`, …), `getattr` with a
   computed name or on the import machinery, a `getattr` alias, a call whose
   callee is not statically nameable (`getattr(m, n)(...)`,
   `globals()["x"]()`), banned tokens appearing as string data, and strings
   reassembled from fragments (`"__imp" "ort__"`, `"ev" + "al"`). The one
   carve-out is `re.compile`. **A protected `.py` that does not parse fails
   closed**: it gets a token-scan fallback plus an `unparseable-python`
   banned construct. Pre-existing occurrences don't re-flag (new-set minus
   old-set); if the *old* side is unparseable, everything on the new side
   counts as newly introduced.
   For `.ts`/`.tsx`, a computed (non-string-literal, including template
   literal) `import(...)` is banned; string-literal `import('./x')` is
   allowed.
5b. **Stricter reachability mode** (`[modes] strict_reachability`, default
   `false`, ships OFF): when on, every promotion target is *also* emitted as
   a banned construct `unpromoted-reference:<path>` — a protected file may
   not reference a module that is not already protected.
6. **Tier logic**:
   - any protected hit / promotion / banned construct / failed gate → **Tier 2**
   - else inert paths only (`[inert]` patterns) + gates pass → **Tier 0**
   - else gates pass and `lines_added + lines_removed <= size_cap` → **Tier 1**
     (dependency files are in the protected list, so a dependency-file
     change can never reach this branch)
   - anything else → **Tier 2**
   - **fail-closed**: an empty `--gates` object is treated as failing.

## Promotion flows through the gate

The classifier only **emits** `proposed_promotions` — it never edits
`protected-paths.toml`. The human approval that merges a Tier 2 diff also
commits the list update via the broker (WP-H3), so the promotion itself
passes through the same gate as the diff that caused it.

Similarly, `daily_auto_apply_budget` in `[limits]` is just the plink-owned
number; counting and escalation live in the broker/orchestrator
(WP-H3/H12), not here.

## Tests

```sh
server/.venv/bin/python -m pytest harness/classifier/tests/ -q
```

Fixture git repos are built in tmpdirs; no fixture touches the live repo,
the running service, or `server/data/`.

`tests/test_bypass_h13.py` is the red-team regression suite for findings
H13-D1/D2/D3 — one test per named evasion, each verified red against the
pre-fix classifier. Treat it as part of the spec: if a change to the
resolver or the ban makes one of those go green-by-weakening, the change is
wrong.
