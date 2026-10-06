"""The why-trail: one step per top-level tool call, labelled by its target
only (SPECS/2026-10-06-why-trail.md, label rules for Gable).

The server checks shape and size and verifies nothing, so this module is the
privacy wall: a label never carries Bash arguments, a Grep pattern, an Agent
prompt, a broker body, or anything a tool returned."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from datetime import datetime
from typing import Any, Optional

__all__ = ["TraceRecorder", "step_for", "MAX_STEPS", "MAX_TRACE_CHARS"]

MAX_STEPS = 50
MAX_LABEL = 160
MAX_TRACE_CHARS = 12000

HOME = "/home/resident"
REPO_ROOTS = ("/opt/disjorn", HOME + "/disjorn")
OWN_REPOS = HOME + "/bots"

WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
AGENT_TOOLS = ("Agent", "Task")
WEB_TOOLS = ("WebFetch", "WebSearch")

REFUSED_BY_BROKER = "refused-by-broker"
# The broker CLI's exit codes and error codes for a call the broker said no to.
BROKER_REFUSAL_EXITS = {10, 11, 12, 13, 20}
BROKER_REFUSAL_CODES = {"unknown-caller", "unknown-verb", "verb-disabled",
                        "bad-args", "broker-disabled-local"}
BROKER_TARGET_FLAGS = ("--range", "--slug", "--spec", "--seq")
BROKER_TARGET_KEYS = ("range", "branch", "slug", "spec", "seq")

_SEPARATORS = {"|", "||", "&&", ";", "&", "|&", "(", ")", ";;"}
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time",
             "{", "}", "fi", "done", "esac"}
_LOOP_HEADS = {"for", "case", "select"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")
_VERB = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_TARGET = re.compile(r"^[A-Za-z0-9._~^/{}#:-]{1,100}$")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_EXIT = re.compile(r"\bExit code (\d+)")
_CODE = re.compile(r'"code"\s*:\s*"([a-z-]+)"')


def _clip(text: str) -> str:
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text if len(text) <= MAX_LABEL else text[:MAX_LABEL - 1] + "…"


def _path(raw: Any, cwd: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raw = "."
    p = raw.strip()
    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    p = posixpath.normpath(posixpath.join(cwd, p))
    for root in REPO_ROOTS:
        if p == root:
            return "."
        if p.startswith(root + "/"):
            return p[len(root) + 1:]
    if p.startswith(OWN_REPOS + "/"):
        return p[len(OWN_REPOS) + 1:]
    if p == HOME:
        return "~"
    if not p.startswith(HOME + "/"):
        return p
    parts = p[len(HOME) + 1:].split("/")
    if "memory" in parts:
        rest = parts[parts.index("memory") + 1:]
        if not rest:
            return "~/memory"
        slug = posixpath.splitext(rest[0])[0]
        return f"~/memory/{slug}" + ("/…" if len(rest) > 1 else "")
    return f"~/{parts[0]}" + ("/…" if len(parts) > 1 else "")


def _strip_heredocs(command: str) -> str:
    kept: list[str] = []
    waiting: list[str] = []
    for line in command.replace("\\\n", " ").split("\n"):
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            continue
        kept.append(line)
        waiting = [m.group(2) for m in _HEREDOC.finditer(line)]
    return " ; ".join(kept)


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(_strip_heredocs(command), posix=True,
                        punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _program(segment: list[str]) -> tuple[Optional[str], list[str]]:
    words = list(segment)
    while words and (words[0] in _KEYWORDS or _ASSIGNMENT.match(words[0])):
        words.pop(0)
    if not words or words[0] in _LOOP_HEADS:
        return None, []
    name = posixpath.basename(words[0])
    return (name if _PROGRAM.match(name) else None), words[1:]


def _broker_label(verb: Any, targets: list[Any]) -> str:
    verb = str(verb) if verb is not None else ""
    if not _VERB.match(verb):
        return "broker"
    shown = [str(t) for t in targets if _TARGET.match(str(t))]
    return " ".join(["broker", verb, *shown])


def _broker_cli(args: list[str]) -> str:
    verb, targets, i = None, [], 0
    while i < len(args):
        word = args[i]
        flag, eq, value = word.partition("=")
        if flag == "--socket":
            i += 1 if eq else 2
            continue
        if verb is None and not word.startswith("-"):
            verb = word
        elif verb is not None and flag in BROKER_TARGET_FLAGS:
            if not eq and i + 1 < len(args):
                i += 1
                value = args[i]
            targets.append(value)
        i += 1
    return _broker_label(verb, targets)


def _bash(command: Any) -> tuple[str, str]:
    if not isinstance(command, str):
        return "shell", "bash"
    try:
        segments = _segments(command)
    except ValueError:
        head = command.strip().split(None, 1)
        segments = [head[:1]] if head else []
    programs: list[str] = []
    brokers: list[str] = []
    for segment in segments:
        name, args = _program(segment)
        if name == "broker":
            brokers.append(_broker_cli(args))
        if name and name not in programs:
            programs.append(name)
    if brokers:
        return "broker", "; ".join(brokers)
    return "shell", "bash: " + ", ".join(programs) if programs else "bash"


def _mcp_broker(name: str, tool_input: dict) -> Optional[str]:
    parts = name.split("__")
    if len(parts) != 3 or "broker" not in parts[1].lower():
        return None
    targets = [tool_input.get(k) for k in BROKER_TARGET_KEYS
               if tool_input.get(k) is not None]
    return _broker_label(parts[2].replace("_", "-"), targets)


def step_for(name: Any, tool_input: Any, cwd: str = HOME) -> tuple[str, str]:
    """(kind, label) for one tool call, read from its target argument only."""
    name = name if isinstance(name, str) else ""
    args = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        kind, label = _bash(args.get("command"))
    elif name in ("Read", *WRITE_TOOLS):
        path = _path(args.get("file_path") or args.get("notebook_path"), cwd)
        kind = "memory" if path.startswith("~/memory") else (
            "read" if name == "Read" else "write")
        label = f"{name.lower()} {path}"
    elif name == "Grep":
        kind, label = "search", f"grep {_path(args.get('path'), cwd)}"
    elif name == "Glob":
        kind, label = "search", f"glob {args.get('pattern') or ''}"
    elif name in AGENT_TOOLS:
        kind, label = "agent", f"agent {args.get('description') or ''}"
    elif name in WEB_TOOLS:
        kind, label = "web", name
    else:
        broker = _mcp_broker(name, args)
        kind, label = ("broker", broker) if broker else ("other", name or "tool")
    return kind, _clip(label)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _broker_refused(content: Any) -> bool:
    text = _result_text(content)
    code = _EXIT.search(text)
    if code and int(code.group(1)) in BROKER_REFUSAL_EXITS:
        return True
    return any(m.group(1) in BROKER_REFUSAL_CODES for m in _CODE.finditer(text))


def _stamp(event: dict) -> Optional[float]:
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ms(start: tuple, end: tuple) -> int:
    for a, b in zip(start, end):
        if a is not None and b is not None:
            return max(0, round((b - a) * 1000))
    return 0


class TraceRecorder:
    """Fed every stream-json event; pairs each top-level tool_use with its
    tool_result by id. Subagent events are skipped, so an Agent call is one
    step however much its subagent did."""

    def __init__(self) -> None:
        self.cwd = HOME
        self._steps: list[dict] = []
        self._open: dict[str, tuple[dict, tuple]] = {}
        self._seen: set[str] = set()

    @property
    def total(self) -> int:
        return len(self._steps)

    def observe(self, event: dict, at: Optional[float] = None) -> None:
        if event.get("parent_tool_use_id"):
            return
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            cwd = event.get("cwd")
            if isinstance(cwd, str) and cwd.startswith("/"):
                self.cwd = cwd
            return
        msg = event.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if etype not in ("assistant", "user") or not isinstance(content, list):
            return
        when = (_stamp(event), at)
        for block in content:
            if not isinstance(block, dict):
                continue
            if etype == "assistant" and block.get("type") == "tool_use":
                self._start(block, when)
            elif etype == "user" and block.get("type") == "tool_result":
                self._finish(block, when)

    def _start(self, block: dict, when: tuple) -> None:
        tool_id = block.get("id")
        if not isinstance(tool_id, str) or tool_id in self._seen:
            return
        self._seen.add(tool_id)
        kind, label = step_for(block.get("name"), block.get("input"), self.cwd)
        # Error until its result says otherwise: a call the session never
        # finished did not succeed.
        step = {"kind": kind, "label": label, "outcome": "error", "ms": 0}
        self._steps.append(step)
        self._open[tool_id] = (step, when)

    def _finish(self, block: dict, when: tuple) -> None:
        opened = self._open.pop(block.get("tool_use_id"), None)
        if opened is None:
            return
        step, started = opened
        step["ms"] = _ms(started, when)
        if block.get("is_error") is not True:
            step["outcome"] = "ok"
        elif step["kind"] == "broker" and _broker_refused(block.get("content")):
            step["outcome"] = "refused"
            step["reason"] = REFUSED_BY_BROKER

    def payload(self) -> dict:
        """{steps, total}: the first MAX_STEPS steps, cut further until the
        server's size measure fits, with total still the true count."""
        steps = [dict(s) for s in self._steps[:MAX_STEPS]]
        while steps and _measure(steps, self.total) > MAX_TRACE_CHARS:
            steps.pop()
        return {"steps": steps, "total": self.total}


def _measure(steps: list[dict], total: int) -> int:
    dumped = [{"kind": s["kind"], "label": s["label"], "outcome": s["outcome"],
               "reason": s.get("reason"), "ms": s["ms"]} for s in steps]
    return len(json.dumps({"steps": dumped, "total": total}))
