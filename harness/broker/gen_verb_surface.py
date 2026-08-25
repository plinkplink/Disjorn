#!/usr/bin/env python3
"""gen_verb_surface.py — GENERATE every seat's broker-verb schema from
verbs.toml + verb_surface.toml. SPECS/2026-08-14-file-vision.md item 3.

    python3 gen_verb_surface.py check                  # exit 1 on drift
    python3 gen_verb_surface.py emit-cli   [--out P]   # shell seat (broker CLI)
    python3 gen_verb_surface.py emit-tools [--out P]   # bot seat   (tool schemas)
    python3 gen_verb_surface.py write                  # rewrite in-repo artifacts

WHAT IS GENERATED AND WHAT IS NOT. The DATA is generated: which verbs exist,
what arguments they take, what a caller is told about them. The BEHAVIOUR is
hand-written and stays where it lives — the CLI's validator, the bot's handler,
and above all the broker's own server-side validation, which is the only
authority any of this ever had. Generating the data is what kills the drift
class; generating the behaviour would only move it.

TWO SEATS, TWO SHAPES, ONE SOURCE:

  * A SHELL seat (Gable, the keyboard) reaches the broker through
    harness/cc/broker-cli/broker. Its "schema" is a set of argparse
    subcommands. The CLI is COPY'd into the resident image as a single
    self-contained file, so the generated table is written INTO it, between
    markers — it cannot import a module that is not in the image.

  * A BOT seat (Claudette) is handed Anthropic tool schemas in her own repo.
    The generated module there is imported by core.py, which registers each
    schema against a generic broker handler.

verbs.toml is the VERB SET and nothing else: it is plink's file, it is the kill
switch, and this generator reads only the KEY NAMES from it (never the
booleans). A verb switched off is still a verb that exists; whether a resident
may call it is decided at the socket, per request, by the broker.

ONE TABLE IS GENERATED FROM, AND IT IS NOT THE ONLY TABLE. Since 2026-08-19
verb_surface.toml also carries [adapter_tools]: a description of tools a bot
seat has from its own adapter code, with no broker and no verbs.toml row
behind them. This file VALIDATES that table (a malformed entry should fail at
a keyboard) and generates NOTHING from it — load_surface does not even return
it. See load_adapter_tools, and the table's own header, for why the inertness
is the point rather than an omission.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent                      # harness/broker -> repo root
VERBS_TOML = HERE / "verbs.toml"
SURFACE_TOML = HERE / "verb_surface.toml"
CLI_PATH = REPO / "harness" / "cc" / "broker-cli" / "broker"

CLI_BEGIN = "# ── BEGIN GENERATED VERB SURFACE ──" + "─" * 38
CLI_END = "# ── END GENERATED VERB SURFACE ──" + "─" * 40

GENERATED_HEADER = (
    "GENERATED FILE — DO NOT EDIT BY HAND.\n"
    "Written by harness/broker/gen_verb_surface.py in the Disjorn repo, from\n"
    "harness/broker/verbs.toml (the verb set) and harness/broker/verb_surface.toml\n"
    "(the caller-facing shape). Change those and regenerate; a hand-patch here is\n"
    "the drift this file exists to end."
)

# Every arg key the grammar knows. An unknown key is a typo that would
# otherwise silently do nothing, which is the failure mode this whole file is
# about — so it is an error.
ARG_KEYS = {
    "type", "required", "flag", "description", "seats", "minimum", "maximum",
    "min_len", "max_len", "pattern", "absolute_path", "no_dotdot",
    "no_leading_dash", "no_nul", "json_arg", "max_json_bytes",
}
VERB_KEYS = {"tool_name", "cli_help", "description", "announce_preface", "args"}
TYPES = {"string", "integer", "object"}

# The adapter-tools table (SPECS/2026-08-19-read-repo-file-rev.md item 3).
# READ ONLY TO BE VALIDATED, NEVER TO BE GENERATED FROM — see load_adapter_tools.
ADAPTER_KEYS = {"seat", "module", "args", "description"}

# Every top-level table verb_surface.toml is allowed to have. A typo'd table
# name (`[adapter_tool.x]`) would otherwise be a section that silently does
# nothing, which is the failure mode this whole file exists to end.
SURFACE_TABLES = {"verbs", "adapter_tools"}

# A verbs.toml section that is a SEAT — the sections a generated schema is made
# from. The kill-switch file also holds the wake caller ([plink]), whose verb no
# seat may call; see verb_names.
SEAT_SECTION_RE = re.compile(r"^res-[a-z][a-z0-9-]*$")


class SurfaceError(Exception):
    """The catalogue and the verb set disagree, or the catalogue is malformed."""


# ── loading ──────────────────────────────────────────────────────────────

def verb_names(verbs_path: Path = VERBS_TOML) -> list[str]:
    """The UNION of verb names over every SEAT section of verbs.toml.

    The union, not an intersection: a verb granted to one resident and not the
    other is a normal state of this house (that is what a per-resident kill
    switch is FOR), and every seat's schema still has to describe it.

    SEAT sections only (`res-<name>`), because since 2026-08-25 verbs.toml also
    carries a non-seat caller: [plink], holding the wake verb. A seat may not
    call `wake` and must not be handed a button for it — a tool in a resident's
    list that the broker refuses by identity is a button wired to a refusal,
    which is the failure this generator exists to prevent in the other
    direction."""
    data = tomllib.loads(verbs_path.read_text(encoding="utf-8"))
    seen: list[str] = []
    for name, section in data.items():
        if not isinstance(section, dict) or not SEAT_SECTION_RE.match(name):
            continue
        for verb in section:
            if verb not in seen:
                seen.append(verb)
    return seen


def load_surface(surface_path: Path = SURFACE_TOML) -> dict:
    """The [verbs] table — THE ONLY THING ANY GENERATED ARTIFACT IS MADE FROM.

    Note what this does NOT return: the [adapter_tools] table, which lives in
    the same file and is deliberately invisible from here. Everything
    downstream — cli_table, tool_schemas, emit_cli_block, emit_tools_module —
    takes this function's result, so adapter tools cannot reach a generated
    schema even by accident. That is the inertness the table's own header
    promises, expressed as the shape of this function rather than as a
    resolution to be careful."""
    data = tomllib.loads(surface_path.read_text(encoding="utf-8"))
    unknown_tables = set(data) - SURFACE_TABLES
    if unknown_tables:
        raise SurfaceError(
            f"{surface_path}: unknown top-level table(s) "
            f"{sorted(unknown_tables)} — expected {sorted(SURFACE_TABLES)}")
    verbs = data.get("verbs")
    if not isinstance(verbs, dict):
        raise SurfaceError(f"{surface_path} has no [verbs] table")
    for verb, entry in verbs.items():
        unknown = set(entry) - VERB_KEYS
        if unknown:
            raise SurfaceError(f"{verb}: unknown key(s) {sorted(unknown)}")
        for key in ("tool_name", "cli_help", "description"):
            if not entry.get(key):
                raise SurfaceError(f"{verb}: missing {key}")
        for arg, spec in (entry.get("args") or {}).items():
            unknown = set(spec) - ARG_KEYS
            if unknown:
                raise SurfaceError(
                    f"{verb}.{arg}: unknown key(s) {sorted(unknown)}")
            if spec.get("type") not in TYPES:
                raise SurfaceError(
                    f"{verb}.{arg}: type must be one of {sorted(TYPES)}")
            if not spec.get("description"):
                raise SurfaceError(f"{verb}.{arg}: missing description")
            for seat in spec.get("seats", ["cli", "tool"]):
                if seat not in ("cli", "tool"):
                    raise SurfaceError(f"{verb}.{arg}: unknown seat {seat!r}")
            if spec.get("pattern"):
                re.compile(spec["pattern"])   # fail here, not in a resident
    return verbs


def load_adapter_tools(surface_path: Path = SURFACE_TOML) -> dict:
    """The [adapter_tools] table — VALIDATED HERE, GENERATED FROM NOWHERE.

    Adapter tools are tools a bot seat has because its own code registers
    them: no socket, no verbs.toml row, no third authority. So this table
    cannot be a grant even in principle, and the one way it could BECOME one
    is by being fed to emit-tools. It is not: the only callers in this module
    are `check` and the line `check` mode prints, neither of which emits
    anything. Read the call sites before adding one.
    tests/test_verb_surface.py asserts the inertness from outside.

    Validating it here is not the same as generating from it: a malformed
    entry should fail at a keyboard, loudly, rather than sit in the catalogue
    describing nothing."""
    data = tomllib.loads(surface_path.read_text(encoding="utf-8"))
    tools = data.get("adapter_tools") or {}
    if not isinstance(tools, dict):
        raise SurfaceError(f"{surface_path}: [adapter_tools] is not a table")
    for name, entry in tools.items():
        unknown = set(entry) - ADAPTER_KEYS
        if unknown:
            raise SurfaceError(
                f"adapter_tools.{name}: unknown key(s) {sorted(unknown)}")
        for key in ("seat", "module", "description"):
            if not entry.get(key):
                raise SurfaceError(f"adapter_tools.{name}: missing {key}")
        args = entry.get("args")
        if not isinstance(args, list) or not all(
                isinstance(a, str) for a in args):
            raise SurfaceError(
                f"adapter_tools.{name}: args must be a list of argument names "
                f"(use [] for a tool that takes none)")
    return tools


# ── the drift check ──────────────────────────────────────────────────────

def check(verbs_path: Path = VERBS_TOML,
          surface_path: Path = SURFACE_TOML) -> list[str]:
    """Every problem, not just the first: a reviewer should see the whole gap
    in one run rather than peel it one verb at a time."""
    problems: list[str] = []
    try:
        surface = load_surface(surface_path)
    except SurfaceError as exc:
        return [str(exc)]
    names = verb_names(verbs_path)
    for name in names:
        if name not in surface:
            problems.append(
                f"verbs.toml grants {name!r} and verb_surface.toml does not "
                f"describe it: no seat can call it. Add a [verbs.{name}] entry.")
    for name in surface:
        if name not in names:
            problems.append(
                f"verb_surface.toml describes {name!r} and verbs.toml has no "
                f"such verb: a button wired to nothing. Remove it, or add the "
                f"switch.")
    tool_names = [e["tool_name"] for e in surface.values()]
    for dup in {n for n in tool_names if tool_names.count(n) > 1}:
        problems.append(f"two verbs share tool_name {dup!r}")
    # The adapter table: its GRAMMAR is checked here, its CONTENTS are checked
    # against the adapter's own core.py by test_verb_surface.py (the two
    # directions of that drift are not visible from this repo alone). Nothing
    # below folds an adapter tool into the verb lists above.
    try:
        adapter = load_adapter_tools(surface_path)
    except SurfaceError as exc:
        return problems + [str(exc)]
    for name in adapter:
        if name in tool_names:
            problems.append(
                f"adapter tool {name!r} has the same name as a broker verb's "
                f"tool: a seat would register two tools under one name and the "
                f"second would win silently. Rename one.")
    return problems


# ── shaping ──────────────────────────────────────────────────────────────

def _arg_flag(arg: str, spec: dict) -> str:
    return spec.get("flag") or "--" + arg.replace("_", "-")


def _for_seat(entry: dict, seat: str) -> dict:
    return {arg: spec for arg, spec in (entry.get("args") or {}).items()
            if seat in spec.get("seats", ["cli", "tool"])}


def cli_table(surface: dict) -> dict:
    """The table the broker CLI builds its parser and its validator from."""
    out: dict = {}
    for verb, entry in surface.items():
        args: dict = {}
        for arg, spec in _for_seat(entry, "cli").items():
            shaped = {"flag": _arg_flag(arg, spec), "type": spec["type"],
                      "required": bool(spec.get("required", False)),
                      "help": spec["description"]}
            for key in ("minimum", "maximum", "min_len", "max_len", "pattern",
                        "absolute_path", "no_dotdot", "no_leading_dash",
                        "no_nul", "json_arg", "max_json_bytes"):
                if key in spec:
                    shaped[key] = spec[key]
            args[arg] = shaped
        out[verb] = {"help": entry["cli_help"], "args": args}
    return out


def tool_schemas(surface: dict) -> list[dict]:
    """Anthropic tool schemas, in catalogue order (= registration order = the
    order they appear in a bot's prompt; see the note in verb_surface.toml)."""
    schemas = []
    for verb, entry in surface.items():
        properties: dict = {}
        required: list[str] = []
        for arg, spec in _for_seat(entry, "tool").items():
            prop: dict = {"type": spec["type"],
                          "description": spec["description"]}
            if "minimum" in spec:
                prop["minimum"] = spec["minimum"]
            if "maximum" in spec:
                prop["maximum"] = spec["maximum"]
            properties[arg] = prop
            if spec.get("required"):
                required.append(arg)
        schemas.append({
            "verb": verb,
            "name": entry["tool_name"],
            "description": entry["description"],
            "input_schema": {"type": "object", "properties": properties,
                             "required": required},
            "announce_preface": bool(entry.get("announce_preface", False)),
            "cli_args": {arg: _arg_flag(arg, spec)
                         for arg, spec in _for_seat(entry, "tool").items()},
            "json_args": sorted(arg for arg, spec
                                in _for_seat(entry, "tool").items()
                                if spec.get("json_arg")),
        })
    return schemas


# ── emitting ─────────────────────────────────────────────────────────────

def _py_literal(obj, indent: int = 0) -> str:
    """A stable, diff-friendly Python literal. json.dumps would be shorter and
    would also emit `true`/`null`, which are not Python."""
    pad = " " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        inner = "".join(
            f"{pad}    {json.dumps(k, ensure_ascii=False)}: "
            f"{_py_literal(v, indent + 4)},\n" for k, v in obj.items())
        return "{\n" + inner + pad + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        inner = "".join(f"{pad}    {_py_literal(v, indent + 4)},\n"
                        for v in obj)
        return "[\n" + inner + pad + "]"
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, float)):
        return repr(obj)
    return json.dumps(obj, ensure_ascii=False)


def emit_cli_block(surface: dict) -> str:
    body = _py_literal(cli_table(surface))
    header = "\n".join("# " + line if line else "#"
                       for line in GENERATED_HEADER.splitlines())
    return (f"{CLI_BEGIN}\n{header}\n"
            f"# The parser and the validator below are hand-written; this table\n"
            f"# is not. Regenerate:\n"
            f"#     python3 harness/broker/gen_verb_surface.py write\n"
            f"VERB_SURFACE = {body}\n{CLI_END}\n")


def emit_tools_module(surface: dict) -> str:
    schemas = tool_schemas(surface)
    names = [s["verb"] for s in schemas]
    return (
        '"""' + GENERATED_HEADER + "\n\n"
        "The broker verbs this seat can reach, as Anthropic tool schemas.\n\n"
        "BROKER_TOOLS is in registration order, which is prompt order.\n"
        "Each entry carries what a generic handler needs to make the call:\n"
        "`verb` (the broker verb), `cli_args` (tool-input key -> CLI flag) and\n"
        "`json_args` (values the CLI wants serialized). Nothing here decides\n"
        "whether a call is ALLOWED — that is verbs.toml, at the socket, per\n"
        "request, and a verb switched off answers verb-disabled as it always\n"
        'did.\n"""\n\n'
        "from __future__ import annotations\n\n"
        f"SOURCE_VERBS = {_py_literal(names)}\n\n"
        f"BROKER_TOOLS = {_py_literal(schemas)}\n\n"
        "BROKER_TOOLS_BY_NAME = {t[\"name\"]: t for t in BROKER_TOOLS}\n"
    )


def write_cli(surface: dict, cli_path: Path = CLI_PATH) -> bool:
    """Replace the generated region in the CLI. Returns True if it changed."""
    text = cli_path.read_text(encoding="utf-8")
    begin = text.find(CLI_BEGIN)
    end = text.find(CLI_END)
    if begin < 0 or end < 0:
        raise SurfaceError(
            f"{cli_path} has no generated region — expected the marker lines "
            f"{CLI_BEGIN!r} and {CLI_END!r}")
    # Cut at the END of the end-marker LINE, not at the end of the marker
    # string: the two differ whenever the rule of box-drawing dashes is a
    # character longer or shorter than the constant, and slicing by length
    # then leaves a stray `─` in the file — which is a Python syntax error,
    # i.e. every seat loses its hands at once.
    tail = text.find("\n", end)
    tail = len(text) if tail < 0 else tail + 1
    new = text[:begin] + emit_cli_block(surface) + text[tail:]
    if new == text:
        return False
    cli_path.write_text(new, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_verb_surface.py",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("mode",
                        choices=["check", "emit-cli", "emit-tools", "write"])
    parser.add_argument("--verbs", type=Path, default=VERBS_TOML)
    parser.add_argument("--surface", type=Path, default=SURFACE_TOML)
    parser.add_argument("--cli", type=Path, default=CLI_PATH)
    parser.add_argument("--out", type=Path, default=None)
    ns = parser.parse_args(argv)

    problems = check(ns.verbs, ns.surface)
    if problems:
        for p in problems:
            print(f"verb-surface drift: {p}", file=sys.stderr)
        return 1
    if ns.mode == "check":
        print(f"verb surface: {len(load_surface(ns.surface))} verbs, "
              f"every one described and switched; "
              f"{len(load_adapter_tools(ns.surface))} adapter tools described "
              f"and generated from nowhere")
        return 0

    surface = load_surface(ns.surface)
    if ns.mode == "write":
        changed = write_cli(surface, ns.cli)
        print(f"{ns.cli}: {'rewritten' if changed else 'already current'}")
        if ns.out:
            ns.out.write_text(emit_tools_module(surface), encoding="utf-8")
            print(f"{ns.out}: written")
        return 0
    text = (emit_cli_block(surface) if ns.mode == "emit-cli"
            else emit_tools_module(surface))
    if ns.out:
        ns.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
