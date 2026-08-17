"""The generated verb surface — SPECS/2026-08-14-file-vision.md item 3.

The spec's original item 3 was "add refresh_mirror to both residents' tool
schemas". Cross-lane review upgraded it: GENERATE the schemas, so the class
dies rather than the instance. These are the tests that make the generation
worth having, because a generator nobody runs is a hand-written file with
extra steps:

  * the CHECK catches both directions of drift, and it is the direction that
    reads as "nothing wrong" that matters — a verb switched ON with no surface
    is a capability a resident cannot reach, and it is INVISIBLE from every
    seat except the one that cannot use it;
  * the CLI's checked-in table is the table the generator produces right now,
    so `git status` is the drift alarm;
  * the shapes the two seats get are the shapes their consumers expect —
    argparse on one side, an Anthropic tool schema on the other.

WHAT IS NOT ASSERTED HERE: what any verb DOES. That is every other file in
this directory. This one only asserts that the description of the surface and
the switches on the surface agree.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gen_verb_surface as gen  # noqa: E402

REPO = Path(gen.__file__).resolve().parent.parent.parent


# ── the check, both directions ───────────────────────────────────────────

def test_the_shipped_catalogue_and_the_shipped_switches_agree():
    assert gen.check() == []


def test_a_verb_with_no_surface_is_caught(tmp_path):
    """The invisible direction: plink flips a switch, the resident has no way
    to press it, and nothing anywhere says so. `start-build` lived like this
    from 2026-08-05 until Claudette reported it herself."""
    verbs = tmp_path / "verbs.toml"
    verbs.write_text('[res-claudette]\n"read-metrics" = true\n'
                     '"summon-the-kraken" = true\n')
    problems = gen.check(verbs, gen.SURFACE_TOML)
    assert any("summon-the-kraken" in p and "no seat can call it" in p
               for p in problems), problems


def test_a_surface_with_no_verb_is_caught(tmp_path):
    """The other direction: a button wired to nothing. `restart_disjorn` sat in
    a tool list for two weeks while verbs.toml had it false, and the only way
    to find out was to press it and drop every human on the server."""
    verbs = tmp_path / "verbs.toml"
    verbs.write_text('[res-claudette]\n"read-metrics" = true\n')
    problems = gen.check(verbs, gen.SURFACE_TOML)
    assert any("button wired to nothing" in p for p in problems)


def test_the_verb_set_is_the_union_over_residents(tmp_path):
    """A verb granted to one resident and not the other is normal — that is
    what a per-resident kill switch is FOR — and both seats' schemas still have
    to describe it. An intersection would silently delete Gable's tools every
    time Claudette got something first."""
    verbs = tmp_path / "verbs.toml"
    verbs.write_text('[res-claudette]\n"a" = true\n"b" = false\n'
                     '[res-gable]\n"b" = true\n"c" = false\n')
    assert gen.verb_names(verbs) == ["a", "b", "c"]


def test_the_booleans_are_never_read(tmp_path):
    """The generator reads NAMES. Whether a resident may call a verb is decided
    at the socket, per request, by the broker — if generation depended on the
    switch, flipping one off would silently delete a tool instead of denying a
    call, and 'verb-disabled' would become 'unknown-verb'."""
    on = tmp_path / "on.toml"
    off = tmp_path / "off.toml"
    on.write_text('[res-claudette]\n"a" = true\n"b" = true\n')
    off.write_text('[res-claudette]\n"a" = false\n"b" = false\n')
    assert gen.verb_names(on) == gen.verb_names(off)


# ── the catalogue's own grammar ──────────────────────────────────────────

@pytest.mark.parametrize("body,fragment", [
    ('[verbs.x]\ntool_name = "x"\ncli_help = "h"\n', "missing description"),
    ('[verbs.x]\ntool_name = "x"\ndescription = "d"\n', "missing cli_help"),
    ('[verbs.x]\ntool_name = "x"\ncli_help = "h"\ndescription = "d"\n'
     'tool_nme = "typo"\n', "unknown key"),
    ('[verbs.x]\ntool_name = "x"\ncli_help = "h"\ndescription = "d"\n'
     '[verbs.x.args.a]\ntype = "sting"\ndescription = "d"\n', "type must be"),
    ('[verbs.x]\ntool_name = "x"\ncli_help = "h"\ndescription = "d"\n'
     '[verbs.x.args.a]\ntype = "string"\n', "missing description"),
])
def test_a_malformed_catalogue_fails_at_generation_not_in_a_resident(
        tmp_path, body, fragment):
    """Every one of these would otherwise surface as a tool that quietly does
    not work, in a container, hours later."""
    surface = tmp_path / "verb_surface.toml"
    surface.write_text(body)
    with pytest.raises(gen.SurfaceError) as exc:
        gen.load_surface(surface)
    assert fragment in str(exc.value)


def test_an_unknown_arg_key_is_an_error_not_a_no_op(tmp_path):
    """A typo'd rule (`maxlen` for `max_len`) would validate nothing and look
    exactly like a rule that passes."""
    surface = tmp_path / "verb_surface.toml"
    surface.write_text('[verbs.x]\ntool_name = "x"\ncli_help = "h"\n'
                       'description = "d"\n[verbs.x.args.a]\n'
                       'type = "string"\ndescription = "d"\nmaxlen = 3\n')
    with pytest.raises(gen.SurfaceError, match="maxlen"):
        gen.load_surface(surface)


# ── the checked-in artifacts are what the generator produces ─────────────

def test_the_cli_table_in_the_repo_is_current():
    """`git status` is the drift alarm. If this fails, run:
        python3 harness/broker/gen_verb_surface.py write"""
    surface = gen.load_surface()
    text = gen.CLI_PATH.read_text(encoding="utf-8")
    assert gen.emit_cli_block(surface) in text, (
        "harness/cc/broker-cli/broker is out of date with verb_surface.toml — "
        "regenerate with: python3 harness/broker/gen_verb_surface.py write")


def test_generation_is_idempotent(tmp_path):
    """A generator that rewrites its own output differently every run makes
    every diff unreadable and the alarm above useless."""
    surface = gen.load_surface()
    copy = tmp_path / "broker"
    copy.write_text(gen.CLI_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    assert gen.write_cli(surface, copy) is False
    assert copy.read_text(encoding="utf-8") == \
        gen.CLI_PATH.read_text(encoding="utf-8")


def test_the_regenerated_cli_still_imports_and_still_parses(tmp_path):
    """The generated region sits inside executable Python. A table that is
    valid data and invalid syntax takes every seat's hands away at once, and
    the failure is at import, in a container, on the next call."""
    proc = subprocess.run(
        [sys.executable, str(gen.CLI_PATH), "--socket",
         "/nonexistent/broker.sock", "refresh-mirror"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3, proc.stderr          # transport, not syntax
    assert "transport" in proc.stdout


# ── the two seats get the shapes their consumers expect ──────────────────

def test_every_verb_reaches_both_seats():
    surface = gen.load_surface()
    cli = gen.cli_table(surface)
    tools = {t["verb"] for t in gen.tool_schemas(surface)}
    assert set(cli) == tools == set(surface)


def test_a_cli_only_arg_is_not_handed_to_a_model():
    """read-own-log --path is a debugging affordance for a shell: the broker
    realpath-pins it to the caller's own log, so it can only ever name the file
    the verb would have read anyway. There is no reason to spend a model's
    attention on it."""
    surface = gen.load_surface()
    assert "path" in gen.cli_table(surface)["read-own-log"]["args"]
    tool, = [t for t in gen.tool_schemas(surface) if t["verb"] == "read-own-log"]
    assert "path" not in tool["input_schema"]["properties"]
    assert "path" not in tool["cli_args"]


def test_tool_schemas_are_anthropic_shaped():
    for tool in gen.tool_schemas(gen.load_surface()):
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["name"].replace("_", "-") == tool["verb"]
        assert tool["description"].strip() == tool["description"]


def test_refresh_mirror_reaches_a_bot_seat():
    """The instance the spec was filed about: ON in verbs.toml for both
    residents since 2026-07, and absent from the bot tool schema until now."""
    tools = {t["name"] for t in gen.tool_schemas(gen.load_surface())}
    assert "refresh_mirror" in tools


def test_the_catalogue_describes_the_gatehouse_fetch_to_its_reader():
    """The verb's behaviour changed under a resident who already had it. The
    description is the only place she finds that out."""
    surface = gen.load_surface()
    description = surface["refresh-mirror"]["description"]
    assert "refs/gatehouse/" in description
    assert "loop/" in description


# ── the catalogue and the deployed template stay honest ──────────────────

def test_the_repo_verbs_template_ships_every_verb_off():
    """Unrelated to generation and worth re-asserting from here: the generator
    reads this file, and a template that shipped something ON would be a grant
    arriving through a code path nobody reviews as a grant."""
    data = tomllib.loads(gen.VERBS_TOML.read_text(encoding="utf-8"))
    for resident, section in data.items():
        for verb, enabled in section.items():
            assert enabled is False, f"{resident}.{verb} ships ON"
