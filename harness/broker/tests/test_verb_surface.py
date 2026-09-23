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
from typing import NamedTuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gen_verb_surface as gen  # noqa: E402


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
    """Generation reads names only: a switch turned off denies a call, it
    never deletes a tool."""
    on = tmp_path / "on.toml"
    off = tmp_path / "off.toml"
    on.write_text('[res-claudette]\n"a" = true\n"b" = true\n')
    off.write_text('[res-claudette]\n"a" = false\n"b" = false\n')
    assert gen.verb_names(on) == gen.verb_names(off)


# ── one seat's tools ─────────────────────────────────────────────────────

LIVE_SHAPED = ('[res-claudette]\n"restart-disjorn" = false\n'
               '"changed-files" = true\n"read-metrics" = true\n'
               '[res-gable]\n"apps-build" = true\n[server]\n"build" = true\n')


def test_a_seat_gets_exactly_the_verbs_its_section_lists(tmp_path):
    verbs = tmp_path / "verbs.toml"
    verbs.write_text(LIVE_SHAPED)
    surface = gen.load_surface()
    seat = gen.seat_surface(verbs, "res-claudette")
    assert list(seat) == [v for v in surface
                          if v in {"restart-disjorn", "changed-files",
                                   "read-metrics"}]
    names = {t["name"] for t in gen.tool_schemas(seat)}
    assert "restart_disjorn" in names
    assert not names & {"apps_build", "summon_hop"}


def test_a_seat_filter_reads_names_not_booleans(tmp_path):
    on = tmp_path / "on.toml"
    off = tmp_path / "off.toml"
    on.write_text('[res-claudette]\n"read-metrics" = true\n')
    off.write_text('[res-claudette]\n"read-metrics" = false\n')
    assert gen.seat_surface(on, "res-claudette") == \
        gen.seat_surface(off, "res-claudette")


@pytest.mark.parametrize("seat", ["res-nobody", "server", "plink"])
def test_a_seat_that_is_not_a_listed_seat_section_is_refused(tmp_path, seat):
    verbs = tmp_path / "verbs.toml"
    verbs.write_text(LIVE_SHAPED + '[plink]\n"wake" = false\n')
    with pytest.raises(gen.SurfaceError, match="no seat section"):
        gen.seat_surface(verbs, seat)


def test_a_listed_verb_with_no_surface_is_refused(tmp_path):
    verbs = tmp_path / "verbs.toml"
    verbs.write_text('[res-claudette]\n"summon-the-kraken" = true\n')
    with pytest.raises(gen.SurfaceError, match="summon-the-kraken"):
        gen.seat_surface(verbs, "res-claudette")


def test_emit_tools_for_one_seat_writes_an_importable_module(tmp_path):
    verbs = tmp_path / "verbs.toml"
    out = tmp_path / "broker_tools.py"
    verbs.write_text(LIVE_SHAPED)
    assert gen.check(verbs) != []
    assert gen.main(["emit-tools", "--verbs", str(verbs), "--seat",
                     "res-claudette", "--out", str(out)]) == 0
    module: dict = {}
    exec(out.read_text(encoding="utf-8"), module)
    assert module["SOURCE_VERBS"] == [
        v for v in gen.load_surface()
        if v in {"restart-disjorn", "changed-files", "read-metrics"}]
    assert "[res-claudette] lists in verbs.toml" in module["__doc__"]
    assert set(module["BROKER_TOOLS_BY_NAME"]) == {
        t["name"] for t in module["BROKER_TOOLS"]}


def test_emit_tools_for_an_unknown_seat_fails_and_writes_nothing(tmp_path,
                                                                 capsys):
    verbs = tmp_path / "verbs.toml"
    out = tmp_path / "broker_tools.py"
    verbs.write_text(LIVE_SHAPED)
    assert gen.main(["emit-tools", "--verbs", str(verbs), "--seat",
                     "res-claudete", "--out", str(out)]) == 1
    assert not out.exists()
    assert "no seat section" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["check", "emit-cli", "write"])
def test_seat_is_refused_outside_emit_tools(mode):
    with pytest.raises(SystemExit) as exc:
        gen.main([mode, "--seat", "res-claudette"])
    assert exc.value.code == 2


def test_the_unfiltered_module_is_unchanged_by_the_seat_option():
    module = gen.emit_tools_module(gen.load_surface())
    assert "The broker verbs this seat can reach, as Anthropic" in module


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
    """A generator that is not idempotent makes the alarm above useless."""
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

def test_every_verb_reaches_both_seats_unless_marked_shell_only():
    surface = gen.load_surface()
    cli = gen.cli_table(surface)
    tools = {t["verb"] for t in gen.tool_schemas(surface)}
    assert set(cli) == set(surface)
    assert tools == {v for v, e in surface.items() if e.get("tool", True)}


def test_summon_hop_never_reaches_a_bot_seat_even_when_granted(tmp_path):
    verbs = tmp_path / "verbs.toml"
    verbs.write_text('[res-claudette]\n"summon-hop" = true\n"read-metrics" = true\n')
    assert "summon-hop" in gen.cli_table(gen.load_surface())
    module = gen.emit_tools_module(gen.seat_surface(verbs, "res-claudette"))
    assert "summon_hop" not in module and "read_metrics" in module


def test_a_non_boolean_tool_flag_is_refused(tmp_path):
    surface = tmp_path / "verb_surface.toml"
    surface.write_text('[verbs.x]\ntool_name = "x"\ncli_help = "h"\n'
                       'description = "d"\ntool = "no"\n')
    with pytest.raises(gen.SurfaceError, match="tool must be"):
        gen.load_surface(surface)


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
# The INERTNESS tests below run everywhere. The DRIFT tests read the adapter's
# core.py through one pin, and a pin that does not resolve is red.
#
# The pin names a git object, never a checkout path. The gate sets it to the
# gatehouse claudette.git (run-gates.sh); elsewhere it is the mirror named by
# broker.toml [gate].mirror, else the /opt/disjorn mount. The gatehouse repo's
# root IS the bot directory, so core.py sits at the top of its branch.

ADAPTER_CORE_ENV = "DISJORN_ADAPTER_CORE"       # "<git repo>:<rev>:<path>"
BROKER_CONFIG_ENV = "DISJORN_BROKER_CONFIG"
BROKER_CONFIG_PATH = Path("/etc/disjorn-broker/broker.toml")
ADAPTER_MIRROR_FALLBACK = Path("/opt/disjorn")
ADAPTER_REV = "gatehouse/claudette/disjorn-port"
ADAPTER_PATH = "core.py"


class _Pin(NamedTuple):
    repo: Path
    rev: str
    path: str
    origin: str        # named in every message this pin can produce
    configured: bool   # False only for the built-in default


class _Adapter(NamedTuple):
    source: str
    at: str            # path, commit and pin, named in every drift failure


def _adapter_pin() -> _Pin:
    """ONE location, resolved by rule rather than by trying paths until one
    exists. `[gate].mirror` is the broker's own record of where the mirror is,
    so an already-deployed broker needs no edit to make this suite honest."""
    env = os.environ.get(ADAPTER_CORE_ENV)
    if env:
        parts = env.split(":")
        if len(parts) != 3 or not all(parts):
            pytest.fail(
                f"{ADAPTER_CORE_ENV}={env!r} is not '<git repo>:<rev>:<path>'. "
                f"It names a git object, not a file: e.g. "
                f"'/opt/disjorn:{ADAPTER_REV}:{ADAPTER_PATH}'.")
        return _Pin(Path(parts[0]), parts[1], parts[2],
                    f"${ADAPTER_CORE_ENV}", True)

    config = Path(os.environ.get(BROKER_CONFIG_ENV) or BROKER_CONFIG_PATH)
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        mirror = (data.get("gate") or {}).get("mirror")
    except (OSError, tomllib.TOMLDecodeError):
        mirror = None
    if mirror:
        return _Pin(Path(mirror), ADAPTER_REV, ADAPTER_PATH,
                    f"{config} [gate].mirror", True)
    return _Pin(ADAPTER_MIRROR_FALLBACK, ADAPTER_REV, ADAPTER_PATH,
                "the built-in default", False)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # The mirror is another uid's; without safe.directory it reads as absent.
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60)


def _adapter() -> _Adapter:
    """The adapter's core.py and where it came from. A skip here is red."""
    pin = _adapter_pin()
    proc = _git(pin.repo, "cat-file", "-p", f"{pin.rev}:{pin.path}")
    if proc.returncode == 0:
        sha = _git(pin.repo, "rev-parse", "--verify",
                   f"{pin.rev}^{{commit}}").stdout.strip()
        return _Adapter(proc.stdout, f"{pin.path} at {sha[:12]} ({pin.rev} "
                                     f"in {pin.repo}, from {pin.origin})")
    tried = (f"{pin.rev}:{pin.path} in {pin.repo} (from {pin.origin}); git "
             f"said: {(proc.stderr or proc.stdout).strip()}")
    mirror_present = _git(pin.repo, "rev-parse", "--git-dir").returncode == 0
    if pin.configured or mirror_present:
        pytest.fail(
            f"the adapter's core.py did not resolve: {tried}. A configured "
            f"pin, or a mirror that is present, that does not resolve is red, "
            f"never a skip. Fix the pin; run refresh-mirror if the gatehouse "
            f"branch was never fetched; at the gate, run-gates.sh's stderr "
            f"names the host repo it mounts. "
            f"{ADAPTER_CORE_ENV}='<git repo>:<rev>:<path>' overrides it.")
    pytest.skip(
        f"no pin and no repo mirror on this disk, so adapter-tool drift "
        f"cannot be checked from here: {tried}. The gate sets "
        f"{ADAPTER_CORE_ENV} (run-gates.sh); resident and build containers "
        f"mount the mirror at {ADAPTER_MIRROR_FALLBACK}; the broker box names "
        f"it in {BROKER_CONFIG_PATH}. Set {ADAPTER_CORE_ENV} to check here.")


def _registrations(source: str) -> tuple[dict[str, list[str]], list[int]]:
    """(name -> arg names for each module-level dict literal passed to
    register_tool, lines of register_tool calls on anything else). Static,
    with ast: importing core.py would stand up anthropic and chromadb."""
    tree = ast.parse(source)
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
    unseen: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_tool"
                and node.args):
            continue
        arg = node.args[0]
        schema = (literals.get(arg.id) if isinstance(arg, ast.Name) else None)
        if schema is None:
            unseen.append(node.lineno)
            continue
        props = (schema.get("input_schema") or {}).get("properties") or {}
        registered[schema["name"]] = sorted(props)
    return registered, unseen


def _declared_tools(source: str) -> dict[str, list[str]]:
    return _registrations(source)[0]


def _verb_tools() -> set[str]:
    return {e["tool_name"] for e in gen.load_surface().values()}


def _adapter_only(declared: dict[str, list[str]]) -> dict[str, list[str]]:
    verbs = _verb_tools()
    return {name: args for name, args in declared.items()
            if name not in verbs}


def _verb_literals(source: str) -> list[str]:
    return sorted(set(_declared_tools(source)) & _verb_tools())


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

def test_the_shipped_configuration_does_not_skip_the_drift_checks():
    """A skip here would claim coverage with nothing behind it, and a skip
    reads as green. It runs under the configuration the suite really has,
    e.g. at the gate."""
    try:
        source = _adapter().source
    except pytest.skip.Exception as exc:
        pytest.fail(f"_adapter() skipped under the shipped configuration, so "
                    f"the adapter-drift tests below cover nothing: {exc}")
    assert "register_tool" in source


def test_a_pin_that_does_not_resolve_is_red_rather_than_a_skip(monkeypatch):
    """A cross-repo check that no-ops when the other repo is absent is worse
    than no check: no check does not lie about coverage."""
    monkeypatch.setenv(ADAPTER_CORE_ENV,
                       "/nonexistent/mirror:gatehouse/claudette/nope:core.py")
    with pytest.raises(pytest.fail.Exception) as exc:
        _adapter()
    assert "/nonexistent/mirror" in str(exc.value)
    assert "gatehouse/claudette/nope" in str(exc.value)
    assert ADAPTER_CORE_ENV in str(exc.value)
    assert "red, never a skip" in str(exc.value)
    assert "not an absent adapter" not in str(exc.value)


def test_a_rev_the_mirror_has_never_fetched_is_red_too(monkeypatch):
    """Mirror present, branch gone — harvested, renamed, or never fetched.
    Same answer, because the coverage is equally absent."""
    repo = _adapter_pin().repo
    monkeypatch.setenv(
        ADAPTER_CORE_ENV, f"{repo}:gatehouse/claudette/no-such-branch:core.py")
    with pytest.raises(pytest.fail.Exception) as exc:
        _adapter()
    assert "no-such-branch" in str(exc.value)


def test_the_env_pin_must_name_a_git_object_not_a_file(monkeypatch):
    """The shape the old candidate list taught everyone to expect. A bare
    path accepted here would resolve to nothing and skip."""
    monkeypatch.setenv(ADAPTER_CORE_ENV, "/home/plink/bots/claudette/core.py")
    with pytest.raises(pytest.fail.Exception, match="git repo"):
        _adapter()


def test_the_one_surviving_skip_needs_no_pin_and_no_mirror(
        monkeypatch, tmp_path):
    monkeypatch.delenv(ADAPTER_CORE_ENV, raising=False)
    monkeypatch.setenv(BROKER_CONFIG_ENV, str(tmp_path / "no-broker.toml"))
    monkeypatch.setattr(sys.modules[__name__], "ADAPTER_MIRROR_FALLBACK",
                        tmp_path / "no-mirror")
    with pytest.raises(pytest.skip.Exception) as exc:
        _adapter()
    assert "no repo mirror on this disk" in str(exc.value)
    assert "run-gates.sh" in str(exc.value)
    assert ADAPTER_CORE_ENV in str(exc.value)


SYNTHETIC_CORE = (
    'A = {"name": "tool_a", "input_schema": {"properties": {"y": {}, "x": {}}}}\n'
    'B = {"name": "tool_b"}\n'
    'DRAFT = {"name": "draft"}\n'
    'V = {"name": "refresh_mirror"}\n'
    'register_tool(A, h)\n'
    'register_tool(V, h)\n'
    'if flag:\n'
    '    register_tool(B, h)\n'
    'for t in LOOP:\n'
    '    register_tool(t, h)\n')


def test_the_scan_sees_registered_literals_and_nothing_else():
    """The scan is the basis of every drift test, so it is asserted on a
    source whose answer is known, not on the live file, whose tool list is
    allowed to grow without editing this test."""
    declared, unseen = _registrations(SYNTHETIC_CORE)
    assert declared == {"tool_a": ["x", "y"], "tool_b": [],
                        "refresh_mirror": []}
    assert unseen == [10]
    assert _adapter_only(declared) == {"tool_a": ["x", "y"], "tool_b": []}
    assert _verb_literals(SYNTHETIC_CORE) == ["refresh_mirror"]


def test_no_broker_verb_is_declared_as_a_literal_in_the_adapter():
    """Broker verbs reach the seat from generated broker_tools.py only."""
    adapter = _adapter()
    shadowed = _verb_literals(adapter.source)
    assert not shadowed, (
        f"{adapter.at} declares broker verbs {shadowed} as literals; they "
        f"must come from the generated broker_tools.py, not by hand.")


def test_every_adapter_tool_the_adapter_registers_is_described():
    """The invisible direction: a tool a seat HAS and no catalogue mentions."""
    adapter = _adapter()
    declared = _adapter_only(_declared_tools(adapter.source))
    missing = sorted(set(declared) - set(gen.load_adapter_tools()))
    assert not missing, (
        f"{adapter.at} registers {missing} and verb_surface.toml's "
        f"[adapter_tools] does not describe them. Add an entry each.")


def test_every_described_adapter_tool_actually_exists():
    """The other direction: a described tool the adapter lacks. Harmless the
    way a button wired to nothing is harmless — until someone believes it."""
    adapter = _adapter()
    declared = _adapter_only(_declared_tools(adapter.source))
    phantom = sorted(set(gen.load_adapter_tools()) - set(declared))
    assert not phantom, (
        f"verb_surface.toml describes {phantom} and {adapter.at} registers "
        f"no such tool. Remove the entry, or write the tool.")


def test_the_described_args_are_the_args_the_tool_takes():
    """Tool-level agreement is not enough: `rev` and `sha_only` arrived on a
    tool that already existed, and a names-only table would have gone on
    being correct and useless through that change."""
    adapter = _adapter()
    declared = _adapter_only(_declared_tools(adapter.source))
    for name, entry in gen.load_adapter_tools().items():
        if name not in declared:
            continue                      # the phantom test above owns that
        assert sorted(entry["args"]) == declared[name], (
            f"{name}: verb_surface.toml says args {sorted(entry['args'])}, "
            f"{adapter.at} says {declared[name]}")


def test_a_drift_failure_names_the_adapter_commit(monkeypatch, tmp_path):
    """A cross-repo red names the commit that drifted, not just the file."""
    repo = tmp_path / "adapter"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "core.py").write_text(
        'EXTRA = {"name": "extra_tool"}\nregister_tool(EXTRA, h)\n')
    subprocess.run(["git", "-C", str(repo), "add", "core.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "x"], check=True)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv(ADAPTER_CORE_ENV, f"{repo}:HEAD:core.py")
    with pytest.raises(AssertionError, match=sha[:12]):
        test_every_adapter_tool_the_adapter_registers_is_described()


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
        if resident == "server":  # not a seat; its switch arms the wire itself
            continue
        for verb, enabled in section.items():
            assert enabled is False, f"{resident}.{verb} ships ON"
