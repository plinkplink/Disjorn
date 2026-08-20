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

import ast
import os
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


# ── the adapter-tools table (spec 2026-08-19-read-repo-file-rev item 3) ──
#
# The [verbs] table above describes a surface the BROKER authorizes, and
# verbs.toml is the authorization. [adapter_tools] describes tools a bot seat
# has from its own code — no socket, no verbs.toml row, no third authority
# anywhere. That asymmetry is the whole reason the table has to be inert: for
# a verb, a row here is a description sitting next to a grant; for an adapter
# tool, a row that fed generation would BE the grant, and editing a config
# file would become a new path that hands a bot a tool.
#
# So the tests split in two. The INERTNESS tests below run everywhere and are
# about this repo alone. The DRIFT tests need the adapter repo on disk,
# because the thing they compare against is core.py — they say so and skip
# when it is absent rather than pass quietly.

ADAPTER_CORE_ENV = "DISJORN_ADAPTER_CORE"


def _adapter_core_candidates() -> list[Path]:
    env = os.environ.get(ADAPTER_CORE_ENV)
    if env:
        return [Path(env)]
    return [
        REPO.parent / "claudette" / "core.py",       # the build-clone layout
        REPO / "bots" / "claudette" / "core.py",     # if it ever moves in-repo
        Path.home() / "work" / "claudette" / "core.py",
        Path("/opt/claudette/core.py"),
    ]


def _adapter_core() -> Path:
    for path in _adapter_core_candidates():
        if path.is_file():
            return path
    pytest.skip(
        "the adapter's core.py is not on this disk, so the two directions of "
        "adapter-tool drift cannot be checked from here. Looked at: "
        + ", ".join(str(p) for p in _adapter_core_candidates())
        + f". Set {ADAPTER_CORE_ENV} to point at it.")


def _declared_tools(core_path: Path) -> dict[str, list[str]]:
    """Every tool core.py DECLARES AND REGISTERS, name -> argument names.

    Read statically, with ast, rather than by importing: core.py imports
    anthropic, aiohttp and a chromadb-backed memory package at module level,
    and this repo's test suite has no business standing any of that up to find
    out what a dict literal says.

    A tool counts when it is a module-level dict literal with a "name" key AND
    that variable is passed to register_tool(). Both halves matter: an
    unregistered schema is a draft, and a registration of something that is
    not a literal here (the MEMORY_TOOLS loop) is a tool this file's table
    deliberately does not scope."""
    tree = ast.parse(core_path.read_text(encoding="utf-8"))
    literals: dict[str, dict] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            literals[target.id] = value

    registered: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_tool"
                and node.args
                and isinstance(node.args[0], ast.Name)):
            continue
        schema = literals.get(node.args[0].id)
        if schema is None:
            continue
        props = (schema.get("input_schema") or {}).get("properties") or {}
        registered[schema["name"]] = sorted(props)
    return registered


def _adapter_only(declared: dict[str, list[str]]) -> dict[str, list[str]]:
    """Registered tools minus the broker verbs, which the [verbs] table above
    already covers and the generator already generates."""
    verb_tools = {e["tool_name"] for e in gen.load_surface().values()}
    return {name: args for name, args in declared.items()
            if name not in verb_tools}


def test_the_adapter_table_loads_and_the_shipped_one_is_well_formed():
    tools = gen.load_adapter_tools()
    assert "read_repo_file" in tools
    for name, entry in tools.items():
        assert entry["module"] == "core.py", (
            f"{name}: the table's scope is tools DECLARED in core.py — see its "
            f"header. A tool from another module needs the scope widened and "
            f"the static scan in this file widened with it.")


def test_the_adapter_table_is_inert_to_the_generator(tmp_path):
    """The load-bearing test. An entry here must not be able to reach a
    generated schema, because there is no verbs.toml row behind it to refuse
    the call afterwards — for an adapter tool, generation WOULD be the grant."""
    original = gen.SURFACE_TOML.read_text(encoding="utf-8")
    surface = tmp_path / "verb_surface.toml"
    surface.write_text(
        original + '\n[adapter_tools.summon_the_kraken]\n'
        'seat = "res-claudette"\nmodule = "core.py"\nargs = ["depth"]\n'
        'description = "a tool nobody wrote"\n', encoding="utf-8")

    before = gen.load_surface(gen.SURFACE_TOML)
    after = gen.load_surface(surface)
    assert before == after, "an adapter row changed what load_surface returns"
    assert gen.tool_schemas(after) == gen.tool_schemas(before)
    assert gen.cli_table(after) == gen.cli_table(before)
    assert gen.emit_tools_module(after) == gen.emit_tools_module(before)
    assert gen.emit_cli_block(after) == gen.emit_cli_block(before)
    assert "summon_the_kraken" not in gen.emit_tools_module(after)


def test_no_adapter_tool_reaches_a_generated_tool_schema():
    """The same claim from the other end, against the shipped file: the tools
    the generator emits are broker verbs, exactly and only."""
    emitted = {t["name"] for t in gen.tool_schemas(gen.load_surface())}
    assert emitted.isdisjoint(set(gen.load_adapter_tools()))
    # By name, not by substring: `start-build`'s description tells a resident
    # to read the spec with read_repo_file, and it should go on saying so.
    module = gen.emit_tools_module(gen.load_surface())
    for name in gen.load_adapter_tools():
        assert f'"name": "{name}"' not in module


def test_the_adapter_table_carries_its_own_grants_nothing_sentence():
    """The header's version of this sentence is written for a file that sits
    beside a grant. The adapter table needs its own, written for the case
    where nothing else grants either — so a reader who lands mid-file cannot
    take 'verbs.toml is the real authority' as the reassurance and move on."""
    text = gen.SURFACE_TOML.read_text(encoding="utf-8")
    header, _, adapter = text.partition("ADAPTER TOOLS")
    assert adapter, "the adapter-tools section is gone"
    assert "THIS FILE GRANTS NOTHING" in header
    assert "GRANTS NOTHING — AND UNLIKE [verbs] ABOVE, NOTHING ELSE GRANTS" \
        in adapter
    assert "INERT TO THE GENERATOR" in adapter


def test_an_adapter_tool_may_not_shadow_a_verbs_tool_name(tmp_path):
    """Two registrations under one name is a tool that silently becomes a
    different tool — and which one wins depends on registration order."""
    surface = tmp_path / "verb_surface.toml"
    surface.write_text(
        gen.SURFACE_TOML.read_text(encoding="utf-8")
        + '\n[adapter_tools.refresh_mirror]\nseat = "res-claudette"\n'
        'module = "core.py"\nargs = []\ndescription = "d"\n', encoding="utf-8")
    problems = gen.check(gen.VERBS_TOML, surface)
    assert any("same name as a broker verb" in p for p in problems), problems


@pytest.mark.parametrize("body,fragment", [
    ('[adapter_tools.x]\nseat = "s"\nmodule = "core.py"\nargs = []\n',
     "missing description"),
    ('[adapter_tools.x]\nseat = "s"\nargs = []\ndescription = "d"\n',
     "missing module"),
    ('[adapter_tools.x]\nseat = "s"\nmodule = "core.py"\nargs = []\n'
     'description = "d"\nargz = ["typo"]\n', "unknown key"),
    ('[adapter_tools.x]\nseat = "s"\nmodule = "core.py"\ndescription = "d"\n'
     'args = "path"\n', "list of argument names"),
])
def test_a_malformed_adapter_entry_fails_at_a_keyboard(tmp_path, body, fragment):
    surface = tmp_path / "verb_surface.toml"
    surface.write_text(body)
    with pytest.raises(gen.SurfaceError) as exc:
        gen.load_adapter_tools(surface)
    assert fragment in str(exc.value)


def test_an_unknown_top_level_table_is_caught(tmp_path):
    """`[adapter_tool.x]` — one character — would be a section describing
    nothing, which is the exact shape of failure this whole file exists to
    end."""
    surface = tmp_path / "verb_surface.toml"
    surface.write_text('[verbs]\n[adapter_tool.x]\nseat = "s"\n')
    with pytest.raises(gen.SurfaceError, match="unknown top-level table"):
        gen.load_surface(surface)


# ── the adapter table vs the adapter itself, both directions ─────────────

def test_a_missing_adapter_repo_says_so_instead_of_passing_quietly(monkeypatch):
    """A cross-repo check that silently no-ops when the other repo is absent
    is worse than no check: it reads as a green suite. This one skips, and the
    skip names both the paths it looked at and the way to fix it."""
    monkeypatch.setenv(ADAPTER_CORE_ENV, "/nonexistent/claudette/core.py")
    with pytest.raises(pytest.skip.Exception) as exc:
        _adapter_core()
    assert "/nonexistent/claudette/core.py" in str(exc.value)
    assert ADAPTER_CORE_ENV in str(exc.value)


def test_the_static_scan_finds_the_adapter_tools_and_not_the_broker_ones():
    """The scan is the whole basis of the two drift tests below, so it gets
    asserted rather than assumed: it must see core.py's own tools, and it must
    not see the MEMORY_TOOLS loop (registered from a name, not a literal),
    which this table deliberately does not scope."""
    declared = _declared_tools(_adapter_core())
    assert "read_repo_file" in declared
    assert "brave_search" in declared
    # Broker verbs ARE declared in core.py today and the scan sees them; it is
    # _adapter_only that takes them back out, using the [verbs] table.
    assert "start_build" in declared
    assert set(_adapter_only(declared)) == {"read_repo_file", "brave_search"}

def test_every_adapter_tool_the_adapter_registers_is_described():
    """The invisible direction, ported: a tool a seat HAS and no catalogue
    mentions. For a broker verb that shows up as verbs.toml drift; an adapter
    tool has no verbs.toml row to drift against, so this test is the only
    place it can show up at all."""
    declared = _adapter_only(_declared_tools(_adapter_core()))
    described = gen.load_adapter_tools()
    missing = sorted(set(declared) - set(described))
    assert not missing, (
        f"core.py registers {missing} and verb_surface.toml's [adapter_tools] "
        f"does not describe them — nothing in this house says what shape they "
        f"have. Add an entry each.")


def test_every_described_adapter_tool_actually_exists():
    """The other direction: a described tool the adapter lacks. Harmless in
    the way a button wired to nothing is harmless — right up until someone
    reads the catalogue and believes it."""
    declared = _adapter_only(_declared_tools(_adapter_core()))
    described = gen.load_adapter_tools()
    phantom = sorted(set(described) - set(declared))
    assert not phantom, (
        f"verb_surface.toml describes {phantom} and core.py registers no such "
        f"tool. Remove the entry, or write the tool.")


def test_the_described_args_are_the_args_the_tool_takes():
    """Tool-level agreement is not enough: `rev` and `sha_only` arrived on a
    tool that already existed (2026-08-19), and a table that tracked only
    names would have gone on being correct and useless through that change."""
    declared = _adapter_only(_declared_tools(_adapter_core()))
    for name, entry in gen.load_adapter_tools().items():
        if name not in declared:
            continue                      # the phantom test above owns that
        assert sorted(entry["args"]) == declared[name], (
            f"{name}: verb_surface.toml says args {sorted(entry['args'])}, "
            f"core.py's schema says {declared[name]}")


def test_read_repo_file_is_described_with_its_rev_and_sha_only(tmp_path):
    """The instance this table was filed about."""
    entry = gen.load_adapter_tools()["read_repo_file"]
    assert set(entry["args"]) == {"path", "rev", "sha_only"}
    assert "object store" in entry["description"]
    # Unknown-rev and absent-path being DISTINCT answers is a promise to a
    # reader at 3am, and the catalogue is where she reads it.
    assert "DISTINCT" in entry["description"]


# ── the catalogue and the deployed template stay honest ──────────────────

def test_the_repo_verbs_template_ships_every_verb_off():
    """Unrelated to generation and worth re-asserting from here: the generator
    reads this file, and a template that shipped something ON would be a grant
    arriving through a code path nobody reviews as a grant."""
    data = tomllib.loads(gen.VERBS_TOML.read_text(encoding="utf-8"))
    for resident, section in data.items():
        for verb, enabled in section.items():
            assert enabled is False, f"{resident}.{verb} ships ON"
