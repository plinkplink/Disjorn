"""prose_sweep.py — the sweep that thins a file's prose without lying about it.

Two things can go wrong with a prose sweep, and only one of them is loud. The
loud one is a changed AST: the tool's own --prove catches that, and it is
asserted here too. The quiet one is a FRAGMENT — prose that survives the sweep
having lost the half that made it true. "The status token under `## Status`
(e.g." is not a shorter docstring; it is a wrong one, and nothing downstream
will ever tell you.

So the gate is what these tests are mostly about. `acceptable()` is the single
place that answers "is this a whole thing to say?", and the answers it must
give are enumerated below: a sentence cut at an abbreviation, at an open
paren, on a comma or a colon, on a preposition whose object was deleted, or
carrying the scar of a removed citation (`(§E,`, `(H13-D4, )`, `# :`) is
refused, and refusal means DROPPED, not trimmed further. The corresponding
sentence with its object still attached is kept.

The rest is the two producers that feed the gate — the sentence splitter,
which must not mistake `e.g.` for the end of a thought, and the citation
stripper, which must take the parenthesis with the name it removes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

KEYBOARD = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "keyboard_prose_sweep", KEYBOARD / "prose_sweep.py")
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------

WHOLE = [
    "The status token under `## Status` (e.g. 'confirmed'), lowercased.",
    "The absence branch (§E): a unit that ended with no result.json is a HALT.",
    "None if `[apps].seat_bots` agrees with the table, else how it does not (§B).",
    "A build that never started, because its seat could not run the tests the "
    "spec asks for.",
    "True if `path` IS `root` or sits underneath it.",
    "The child holds its own dups; this process must not.",
    "The transient unit one turn runs in.",
    "A fixed argv list out of `[apps]`, validated like `[commands]` is.",
    "Reserve the budget slot and claim the slug under the lock (H13-D4, BL-D4).",
    "The detached build is NOT a synchronous _run() call.",
    "Builds are capped by default, unlike the action budget, etc.",
    "Has the owner asked for the running turn to stop?",
]

FRAGMENTS = [
    # cut at an abbreviation that promises an example
    "The status token under `## Status` (e.g.",
    "The status token under `## Status` (i.e.",
    # cut on a preposition whose object was deleted
    "A resident cannot exhaust its own cap by.",
    # a citation removed from inside a parenthesis
    "The absence branch (§E,: a unit that ended with no result.json is a HALT.",
    "One flat sentence saying how it does not (§B,.",
    "The one sentence a resident gets to say back (§E,.",
    "Reserve the budget slot and claim the slug under the lock (H13-D4, ).",
    "The push log's sibling (spec, confirmed ).",
    "The Plan Room's third rebuild trigger ( P4).",
    "A parenthesis with nothing left in it ( ).",
    # a citation removed from the head, leaving its punctuation behind
    ": the confirm gate's REAL authorization is that specs_dir is unwritable.",
    "and friends: an unsafe config is a REFUSAL TO START.",
    # stops that are not stops
    "The status token under `## Status`, lowercased,",
    "The status token under `## Status`, lowercased:",
    "The absence branch (§E",
    "A backtick that never closes: `## Status is a heading.",
    # says nothing at all
    "",
    "   ",
    "...",
]


@pytest.mark.parametrize("text", WHOLE)
def test_whole_sentences_are_acceptable(text):
    assert ps.acceptable(text), text


@pytest.mark.parametrize("text", FRAGMENTS)
def test_fragments_are_refused(text):
    assert not ps.acceptable(text), text


def test_refusal_is_a_drop_not_a_trim():
    """The gate's answer is binary. first_sentence() returns nothing rather
    than a shorter fragment, which is what makes 'whole sentence or no
    comment' true instead of aspirational."""
    assert ps.first_sentence("The status token under `## Status` (e.g.") == ""
    assert ps.first_sentence("Cut at the paren (") == ""


# ---------------------------------------------------------------------------
# The sentence splitter.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The status token under `## Status` (e.g. 'confirmed'), lowercased.",
    "A cap i.e. the ceiling on a resident's day, not a rate limit.",
    "The launcher blocks, cf. the spec build, for the whole turn.",
    "One seat vs. another is not the question here.",
    "Absence is a HALT (§E.1) and the record says so.",
    "The timeout is 1.5 seconds, which is a whole lifetime here.",
    "Named for J. Smith, who never worked on it.",
    "Buckets, ledgers, spools, etc. are all under /srv.",
])
def test_these_full_stops_do_not_end_a_sentence(text):
    assert ps.split_sentences(text) == [text]


def test_a_real_full_stop_does_end_a_sentence():
    text = ("The absence branch is a HALT. Absence has to mean something or "
            "it means wait forever.")
    assert ps.split_sentences(text) == [
        "The absence branch is a HALT.",
        "Absence has to mean something or it means wait forever."]


def test_a_stop_inside_a_parenthesis_is_not_a_boundary():
    text = "The cap lives in config (see BUILD-LOOP.md. Not here) and nowhere else."
    assert ps.split_sentences(text) == [text]


# ---------------------------------------------------------------------------
# The citation stripper.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    # the parenthesis goes with the name it held
    ("A unit that ended with no result.json (Claudette #2329) is a HALT.",
     "A unit that ended with no result.json is a HALT."),
    # ...and survives when it held something else too
    ("The absence branch (§E, Claudette #2329): a unit is a HALT.",
     "The absence branch (§E): a unit is a HALT."),
    ("Saying how it does not (§B, Claudette #2293).",
     "Saying how it does not (§B)."),
    ("The third rebuild trigger (seq 1428 P4).",
     "The third rebuild trigger (P4)."),
    ("The push log's sibling (spec 2026-08-27, confirmed seq 2067).",
     "The push log's sibling (spec, confirmed)."),
    # a design-decision label is an anchor, not a citation: it stays, and so
    # does the colon that would otherwise be left orphaned behind it
    ("BL-D2: the build's stdout goes to temp FILES.",
     "BL-D2: the build's stdout goes to temp FILES."),
    ("Reserve the slot under the lock (H13-D4, BL-D4).",
     "Reserve the slot under the lock (H13-D4, BL-D4)."),
    # code is not prose
    ("The detached build is NOT a synchronous _run() call.",
     "The detached build is NOT a synchronous _run() call."),
])
def test_strip_citations(before, after):
    assert ps.strip_citations(before) == after


def test_a_word_broken_across_lines_comes_back_whole():
    assert ps.strip_citations("every narration shape is unit-\n# testable.") == \
        "every narration shape is unit-testable."


# ---------------------------------------------------------------------------
# Banners.
# ---------------------------------------------------------------------------

FRAMED = '''\
# ---------------------------------------------------------------------------
# The broker.
# ---------------------------------------------------------------------------

X = 1
'''

FRAMED_CITATION_ONLY = '''\
# ---------------------------------------------------------------------------
# Claudette #2329
# ---------------------------------------------------------------------------

X = 1
'''


def test_a_banner_keeps_its_name():
    """A frame whose heading is shorter than a comment is worth keeping is
    still a heading: the length floor that applies to prose must not empty a
    banner and leave two rules sandwiching nothing."""
    out = ps.sweep(FRAMED)
    assert "# The broker." in out
    assert out.count("# ---") == 2


def test_a_banner_with_no_words_left_is_dropped_whole():
    out = ps.sweep(FRAMED_CITATION_ONLY)
    assert "# ---" not in out
    assert "X = 1" in out


BANNER_WITH_BODY = '''\
# ----------------------------------------------------- apps-build
#
# THE SHAPE OF THIS VERB, and why it is not start-build with different
# strings. A spec build is one long-running child whose stdout IS the
# evidence; an app build turn is a unit run by ANOTHER seat. So:
#
#   * the launcher blocks for the whole turn.

X = 1
'''


def test_a_banner_heading_keeps_the_paragraph_that_explains_it():
    """One sentence under a banner is a promise the rest of the block was
    keeping. A heading followed by nothing is the fragment problem one level
    up, so a banner keeps its whole first paragraph."""
    flat = " ".join(l.strip().lstrip("#").strip()
                    for l in ps.sweep(BANNER_WITH_BODY).splitlines())
    assert "THE SHAPE OF THIS VERB" in flat
    assert "A spec build is one long-running child" in flat
    # ...but not the sentence that only introduces the bullets it lost
    assert "So:" not in flat
    assert "the launcher blocks" not in flat


# ---------------------------------------------------------------------------
# Docstrings.
# ---------------------------------------------------------------------------

POINTER = '''\
def f(session):
    """Has the owner asked for the running turn to stop? Read off the
    server's harness-view, best-effort: a read that fails answers "not yet"
    and the next poll asks again, because a reaper must not die over a
    question it can repeat.

    Second paragraph, dropped."""
    try:
        return g(session)
    except Exception:  # noqa: BLE001 — see docstring
        return False
'''


def test_a_see_docstring_pointer_still_points_at_something():
    """`# noqa: BLE001 — see docstring` survives the sweep because it is a
    lint directive. If the docstring it names has been reduced to the one
    question it opens with, the pointer now points at nothing, so a docstring
    a comment points at keeps the paragraph rather than the sentence."""
    out = ps.sweep(POINTER)
    assert "see docstring" in out
    assert "a reaper must not die over a question it can repeat" in out
    assert "Second paragraph" not in out


def test_a_docstring_is_never_deleted():
    """Dropping a docstring would drop an AST node; the gate's answer for a
    docstring is therefore 'keep more', never 'keep nothing'."""
    src = 'def f():\n    """cut at the paren ("""\n    return 1\n'
    out = ps.sweep(src)
    assert ps.prove(src, out)
    assert '"""' in out


# ---------------------------------------------------------------------------
# The whole sweep.
# ---------------------------------------------------------------------------

SAMPLE = '''\
"""A module (Claudette #2329).

More prose that goes."""
import os

# BL-D2: the build's stdout/stderr go to temp FILES (bounded on disk), never
# to a pipe the privileged broker must drain into RAM (Gable #2358).
CAP = 1


def f(x):
    """The status token under `## Status` (e.g. 'confirmed'), lowercased, or
    None if the section is absent. Backticks are ignored."""
    return os.path.join(str(x), "y")  # trailing note, kept
'''


def test_the_sweep_never_touches_the_code():
    out = ps.sweep(SAMPLE)
    assert ps.prove(SAMPLE, out)


def test_everything_the_sweep_emits_passes_its_own_gate():
    """The property the reviewer actually cares about: no output of this tool
    is a fragment. Asserted over the sample rather than over any one string,
    because a sweep that is only right on the cases someone thought of is the
    sweep that shipped `(§E,` last time."""
    import ast
    import io
    import tokenize

    out = ps.sweep(SAMPLE)
    lines = out.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(out).readline):
        if tok.type != tokenize.COMMENT:
            continue
        line = lines[tok.start[0] - 1]
        if (not line.strip().startswith("#") or ps.RULE.match(line)
                or ps.BANNER.match(line) or ps.KEEP_DIRECTIVE.search(tok.string)):
            continue
        # a comment block is wrapped over several lines; join it back up
        n = tok.start[0]
        block = []
        while n <= len(lines) and lines[n - 1].strip().startswith("#"):
            block.append(lines[n - 1].strip().lstrip("#").strip())
            n += 1
        assert ps.acceptable(" ".join(block)), block

    for node in ast.walk(ast.parse(out)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                assert ps.acceptable(doc), doc
