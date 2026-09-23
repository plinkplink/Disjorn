"""Prose counter for the code-prose ceiling (SPECS/2026-09-08-prose-standing-orders.md).
The only copy of the arithmetic: the gate test, the digest and the sweep proof all import it."""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

BASELINE_REL = "harness/prose-baseline.toml"
ALLOWANCE_RATIO = 0.25
ALLOWANCE_FLOOR = 1024
SUFFIXES = (".py", ".sh")

# Provenance shapes, numeric only; a date inside a spec slug is a pointer, not a story.
CITATION_RE = re.compile(
    r"#\d{3,}|\bseq\s*#?\s*\d+|(?<![\w/-])\d{4}-\d{2}-\d{2}(?![\w-])", re.IGNORECASE)


@dataclass
class Measure:
    path: str
    total: int
    prose: int
    prose_lines: set[int] = field(default_factory=set)

    @property
    def ratio(self) -> float:
        return self.prose / self.total if self.total else 0.0

    @property
    def allowance(self) -> int:
        return allowance(self.total)

    @property
    def over(self) -> bool:
        return self.prose > self.allowance


def allowance(total_bytes: int) -> int:
    return max(int(total_bytes * ALLOWANCE_RATIO), ALLOWANCE_FLOOR)


def prose_line_numbers(text: str, suffix: str) -> set[int]:
    """1-based line numbers that are prose: pure comment lines, docstring lines."""
    if suffix == ".sh":
        out = set()
        for n, line in enumerate(text.splitlines(), 1):
            s = line.lstrip()
            if s.startswith("#") and not (n == 1 and s.startswith("#!")):
                out.add(n)
        return out
    if suffix != ".py":
        return set()
    out: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT and tok.line.lstrip().startswith("#"):
                out.add(tok.start[0])
    except (tokenize.TokenError, SyntaxError):
        pass
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def measure_text(text: str, suffix: str, path: str = "") -> Measure:
    lines = text.splitlines(keepends=True)
    prose_lines = prose_line_numbers(text, suffix)
    prose = sum(len(lines[n - 1].encode("utf-8")) for n in prose_lines if n <= len(lines))
    return Measure(path=path, total=len(text.encode("utf-8")), prose=prose,
                   prose_lines=prose_lines)


def measure_file(root: Path, rel: str) -> Measure:
    text = (root / rel).read_text(encoding="utf-8", errors="replace")
    return measure_text(text, Path(rel).suffix, rel)


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *[f"*{s}" for s in SUFFIXES]],
                         cwd=root, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p and (root / p).is_file()]


def scan(root: Path, files: Optional[Iterable[str]] = None) -> list[Measure]:
    return [measure_file(root, rel) for rel in (files if files is not None else tracked_files(root))]


def load_baseline(text: str) -> tuple[dict[str, int], dict[str, list[str]]]:
    data = tomllib.loads(text)
    files = {k: int(v) for k, v in data.get("files", {}).items()}
    allow = {k: list(v) for k, v in data.get("citation_allow", {}).items()}
    return files, allow


def render_baseline(measures: Iterable[Measure], allow: dict[str, list[str]]) -> str:
    over = sorted((m for m in measures if m.over), key=lambda m: m.path)
    L = ["# Prose bytes per file at adoption, files over their allowance only.",
         "# A line may be lowered or removed, never raised (harness/tests/test_prose_ratio.py).",
         "", "[files]"]
    L += [f'"{m.path}" = {m.prose}' for m in over]
    L += ["", "# Exact numeric citation text allowed in a file's comments, one list per file.",
          "[citation_allow]"]
    L += [f'"{k}" = [{", ".join(json.dumps(t) for t in v)}]' for k, v in sorted(allow.items())]
    return "\n".join(L) + "\n"


def citation_hits(text: str) -> list[str]:
    return [m.group(0) for m in CITATION_RE.finditer(text)]


def summary(root: Path, baseline_text: Optional[str] = None) -> dict:
    """Digest shape: worst file by ratio and how many files exceed their baseline or allowance."""
    measures = scan(root)
    files, _ = load_baseline(baseline_text) if baseline_text is not None else ({}, {})
    over = [m for m in measures
            if (m.prose > files[m.path] if m.path in files else m.over)]
    worst = max(measures, key=lambda m: m.ratio, default=None)
    return {"worst": worst.path if worst else None,
            "worst_ratio": worst.ratio if worst else 0.0,
            "over": len(over), "files": len(measures)}


def repo_root(start: Optional[Path] = None) -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start or Path.cwd(),
                         capture_output=True, text=True, check=True).stdout.strip()
    return Path(out)


def main(argv: list[str]) -> int:
    root = repo_root(Path(__file__).resolve().parent)
    cmd = argv[0] if argv else "report"
    if cmd == "report":
        for m in sorted(scan(root), key=lambda m: m.ratio, reverse=True)[:int(argv[1]) if len(argv) > 1 else 20]:
            flag = " OVER" if m.over else ""
            print(f"{m.ratio:6.1%} {m.prose:8d} / {m.total:8d}  {m.path}{flag}")
        return 0
    if cmd == "baseline":
        path = root / BASELINE_REL
        allow = load_baseline(path.read_text())[1] if path.exists() else {}
        text = render_baseline(scan(root), allow)
        if "--write" in argv:
            path.write_text(text)
            print(f"wrote {BASELINE_REL}")
        else:
            sys.stdout.write(text)
        return 0
    print("usage: ratio.py report [N] | baseline [--write]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
