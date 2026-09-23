"""The prose ceiling wall (SPECS/2026-09-08-prose-standing-orders.md): allowance for
new files, byte ratchet for baseline files, numeric citation grep on changed prose."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from prose import ratio as R

ROOT = R.repo_root(Path(__file__).resolve().parent)
BASELINE = ROOT / R.BASELINE_REL
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git(*args: str) -> str:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def merge_base() -> str | None:
    head = git("rev-parse", "HEAD")
    for ref in ("main", "origin/main"):
        base = git("merge-base", "HEAD", ref)
        if base and base != head:
            return base
    return None


def added_lines(base: str, rel: str) -> set[int]:
    out = set()
    for line in git("diff", "-U0", base, "--", rel).splitlines():
        m = HUNK_RE.match(line)
        if m:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.update(range(start, start + count))
    return out


# ---- the counter ----------------------------------------------------------

def test_counter_counts_comment_lines_and_docstrings_only():
    src = ('"""Doc line one.\nDoc line two."""\n'
           "x = 1  # trailing comments are code lines\n"
           "# a pure comment\n"
           "def f():\n"
           "    '''inner doc'''\n"
           "    return 'not # a comment'\n")
    m = R.measure_text(src, ".py")
    assert m.prose_lines == {1, 2, 4, 6}
    lines = src.splitlines(keepends=True)
    assert m.prose == sum(len(lines[i - 1].encode()) for i in (1, 2, 4, 6))
    assert m.total == len(src.encode())


def test_counter_sh_skips_shebang_and_counts_indented_comments():
    src = "#!/bin/sh\n# top\n  # indented\necho hi # trailing\n"
    m = R.measure_text(src, ".sh")
    assert m.prose_lines == {2, 3}


def test_allowance_is_quarter_or_one_kb():
    assert R.allowance(100) == 1024
    assert R.allowance(4096) == 1024
    assert R.allowance(40000) == 10000


def test_unparseable_python_still_counts_comments():
    m = R.measure_text("# c\ndef (:\n", ".py")
    assert m.prose_lines == {1}


def test_citation_shapes_are_numeric_only():
    assert R.citation_hits("# ruled by Claudette #2366 at seq 2373 on 2026-09-08") == \
        ["#2366", "seq 2373", "2026-09-08"]
    assert R.citation_hits("# superseded, used to, ruled by — words are not hits") == []
    assert R.citation_hits("# see #12 and step 3") == []
    assert R.citation_hits("# see SPECS/2026-09-08-prose-standing-orders.md") == []


def test_baseline_roundtrip_and_render_lists_only_over_files(tmp_path):
    over = R.Measure("a.py", 10000, 9000)
    under = R.Measure("b.py", 100000, 5000)
    text = R.render_baseline([under, over], {"a.py": ["#123"]})
    files, allow = R.load_baseline(text)
    assert files == {"a.py": 9000}
    assert allow == {"a.py": ["#123"]}


def test_summary_reports_worst_and_over(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "big.py").write_text("# " + "x" * 2000 + "\ny = 1\n")
    (tmp_path / "small.py").write_text("y = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    s = R.summary(tmp_path)
    assert s["worst"] == "big.py" and s["over"] == 1 and s["files"] == 2
    s = R.summary(tmp_path, baseline_text='[files]\n"big.py" = 2003\n')
    assert s["over"] == 0


# ---- the wall -------------------------------------------------------------

@pytest.fixture(scope="module")
def measures() -> dict[str, R.Measure]:
    return {m.path: m for m in R.scan(ROOT)}


@pytest.fixture(scope="module")
def baseline() -> tuple[dict[str, int], dict[str, list[str]]]:
    return R.load_baseline(BASELINE.read_text())


def test_files_outside_the_baseline_are_within_allowance(measures, baseline):
    files, _ = baseline
    bad = [f"{m.path}: {m.prose} prose bytes of {m.total} (allowance {m.allowance})"
           for m in measures.values() if m.path not in files and m.over]
    assert not bad, "prose over allowance; write less prose:\n" + "\n".join(bad)


def test_baseline_files_never_rise_and_lines_stay_true(measures, baseline):
    files, _ = baseline
    rose, stale, gone = [], [], []
    for path, recorded in files.items():
        m = measures.get(path)
        if m is None:
            gone.append(path)
        elif m.prose > recorded:
            rose.append(f"{path}: {m.prose} > baseline {recorded}")
        elif m.prose < recorded:
            stale.append(f'"{path}" = {m.prose}   (baseline still says {recorded})')
    assert not rose, "prose bytes above baseline; write less prose:\n" + "\n".join(rose)
    assert not stale, ("prose fell; lower these lines in harness/prose-baseline.toml:\n"
                       + "\n".join(stale))
    assert not gone, "baseline lists files that no longer exist; remove:\n" + "\n".join(gone)


def test_baseline_lines_are_never_raised(baseline):
    base = merge_base()
    if base is None:
        pytest.skip("no merge-base with main: ratchet direction not checked")
    old_text = git("show", f"{base}:{R.BASELINE_REL}")
    if not old_text:
        pytest.skip("baseline did not exist at the merge-base")
    old, _ = R.load_baseline(old_text)
    new, _ = baseline
    raised = [f"{p}: {old[p]} -> {new[p]}" for p in new if p in old and new[p] > old[p]]
    added = [p for p in new if p not in old]
    assert not raised, "baseline lines raised:\n" + "\n".join(raised)
    assert not added, "new baseline lines; new files must fit their allowance:\n" + "\n".join(added)


def test_changed_prose_carries_no_numeric_citations(measures, baseline):
    base = merge_base()
    if base is None:
        pytest.skip("no merge-base with main (clean main?): citation half skipped")
    _, allow = baseline
    changed = [p for p in git("diff", "--name-only", base, "--", *[f"*{s}" for s in R.SUFFIXES]).splitlines()
               if p in measures]
    hits = []
    for rel in changed:
        m = measures[rel]
        lines = (ROOT / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        allowed = set(allow.get(rel, []))
        for n in sorted(m.prose_lines & added_lines(base, rel)):
            for hit in R.citation_hits(lines[n - 1]):
                if hit not in allowed:
                    hits.append(f"{rel}:{n}: {hit!r} in {lines[n - 1].strip()!r}")
    assert not hits, ("provenance in changed prose; git holds who and when, SPECS/ holds why "
                      "(or add the exact text under [citation_allow]):\n" + "\n".join(hits))
