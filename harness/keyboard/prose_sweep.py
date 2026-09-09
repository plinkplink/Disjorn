"""prose_sweep.py — thin a Python file's prose to its first whole sentences.

    python3 prose_sweep.py <in.py> <out.py>     # rewrites; asserts the AST is
                                                # unchanged minus docstrings
    python3 prose_sweep.py --prove <a.py> <b.py> # exit 0 iff AST-equal minus docs

Every docstring becomes its first whole sentence; every full-line comment
block becomes its first whole sentence, or nothing at all. WHOLE is the whole
rule. A string that stops at `e.g.`, at an open paren, at a comma or on a
preposition is a fragment, and a fragment is worse than no comment, so
`acceptable()` is the one gate every retained string must pass and its answer
is drop, never trim. Seq citations, dates and reviewer names are dropped from
what remains — and the parenthesis they lived in goes with them, so no `(§E,`
is ever left standing where a name used to be. Section banners keep their
heading and the paragraph under it, because a banner framing an empty line is
worse than no banner. Lint directives and shebangs are never touched. The code
is never touched, and --prove is how a reviewer checks that without reading a
deletion diff: it compares the two files' ASTs with docstring values blanked.

Built for the brokerd.py sweep (2026-09-09): 301 KB, 45% prose, over the
custodian seat's 200 KB read cap."""
import ast, io, re, sys, tokenize

# Citations: a reviewer's name and seq, a bare seq, a date. Design-decision
# labels (BL-D2, WP-H12, §E) are NOT citations — they are the anchors the
# prose is written against, and dropping them is what leaves `# :` behind.
CIT = re.compile(
    r"(?:Claudette|Gable|BuildGable|Fable|plink)\s*#\d{3,4}"
    r"|(?<![\w#])#\d{3,4}\b"
    r"|\bseq\s+\d{2,4}\b"
    r"|(?<![/\w-])20\d\d-\d\d(?:-\d\d)?(?![\w-])")
KEEP_DIRECTIVE = re.compile(r"^#!|noqa|pragma|type:\s|fmt:|pylint|ruff|coding[:=]")
# A bare frame rule, and any line that opens a frame (with or without a title).
RULE = re.compile(r"^\s*#\s*[-=─═_*]{6,}\s*$")
BANNER = re.compile(r"^\s*#\s*[-=─═]{6,}|^\s*#\s*[-─═]+\s*\S.*[-─═]{3,}\s*$")
MAX_SENT = 200          # a comment's first sentence
MAX_DOC = 400           # a docstring's first sentence
MAX_PARA = 300          # a banner heading's paragraph
MIN_COMMENT = 12        # below this a surviving comment says nothing

# Abbreviations whose full stop does not end a sentence.
ABBREV = {"e.g", "i.e", "cf", "vs", "etc", "viz", "al", "approx", "resp",
          "fig", "eq", "vol", "pp", "dr", "mr", "mrs", "ms", "jr", "sr", "inc"}
# ...and the subset of those that must never be the LAST thing said.
TERMINAL_ABBREV = ABBREV - {"etc"}
# A sentence ending on one of these ended early: its object was cut away.
PREPOSITIONS = {
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as", "into",
    "onto", "over", "under", "between", "among", "about", "against",
    "during", "without", "within", "upon", "per", "via", "through", "across",
    "toward", "towards", "behind", "beyond", "beside", "around", "unlike",
}
CONJUNCTIONS = {
    "and", "or", "but", "nor", "than", "because", "although", "though",
    "while", "whereas", "unless", "until", "whether", "that", "which", "who",
    "whom", "whose", "if", "when",
}
DETERMINERS = {"a", "an", "the", "its", "their", "our", "your", "my", "every",
               "each"}
DANGLING = PREPOSITIONS | CONJUNCTIONS | DETERMINERS
# English strands prepositions — "the tests the spec asks for." is whole. What
# is NOT whole is a preposition hanging off a noun, which is what a cut object
# leaves: "cannot exhaust its own cap by." So a terminal preposition is allowed
# exactly when the word in front of it is a verb.
VERBISH = {"is", "are", "was", "were", "be", "been", "being", "do", "does",
           "did", "can", "could", "must", "should", "would", "may", "might",
           "will", "shall", "not", "go", "run", "put", "set", "get", "let",
           "make", "take", "look", "come", "deal", "ask", "live", "depend"}
# A sentence opening on one of these lost the clause it was hanging off.
LEAD_ORPHAN = {"and", "or", "but", "nor", "yet", "than", "which", "whom",
               "whose", "namely", "because", "so"}
# Shapes that only ever appear where something was cut out of the middle.
SCARS = [
    re.compile(r"\(\s+\)"),          # ( ) — `_run()` is code and stays
    re.compile(r"\(\s"),             # ( P4
    re.compile(r"\s\)"),             # confirmed )
    re.compile(r"\(\s*[,;:]"),       # (, foo)
    re.compile(r"[,;:]\s*\)"),       # (H13-D4, )
    re.compile(r"[,;:]\s*[.;:]"),    # (§B,.
    re.compile(r"\s[,;:.]"),         # a space before its own punctuation
    re.compile(r"[,;:]{2,}"),
    re.compile(r"^[\s,;:.)\]}]"),    # : the confirm gate's REAL...
]


def _is_abbrev(text: str, i: int) -> bool:
    """True if the full stop at text[i] belongs to an abbreviation, an initial
    or a §-citation rather than to the end of a sentence."""
    if text[i] != ".":
        return False
    j = i
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] in "._§"):
        j -= 1
    raw = text[j:i]
    token = raw.lower().strip("._")
    if not token:
        return False
    if "§" in raw:
        return True
    if token in ABBREV:
        return True
    return len(token) == 1 and token.isalpha()


def _verbish(word: str) -> bool:
    """A rough "is this a verb?" — enough to tell a stranded preposition
    ("the tests the spec asks for") from a cut one ("its own cap by")."""
    if word in VERBISH:
        return True
    return len(word) > 3 and word.endswith(("s", "ed", "ing", "en"))


def _opens_sentence(text: str, k: int) -> bool:
    return k >= len(text) or text[k].isupper() or text[k] in "`\"'(§"


def split_sentences(text: str) -> list:
    """Split on `.`/`?`/`!` — but only outside parens and backticks, only when
    the stop is not an abbreviation, and only before something that can open a
    sentence."""
    text = " ".join(text.split())
    out, start, i, n, depth, ticks = [], 0, 0, len(text), 0, 0
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "`":
            ticks ^= 1
        elif ch in ".!?" and depth == 0 and not ticks and not _is_abbrev(text, i):
            j = i + 1
            while j < n and text[j] in "\"'`”’":
                j += 1
            if j >= n or (text[j] == " " and _opens_sentence(text, j + 1)):
                out.append(text[start:j].strip())
                start = i = j
                continue
        i += 1
    if start < n:
        out.append(text[start:].strip())
    return [s for s in out if s]


def acceptable(text: str) -> bool:
    """The one gate. True only for a whole, unmutilated sentence: it ends on a
    full stop that is really the end, its brackets and backticks close, it
    does not open or close on a word left hanging by a deletion, and it
    carries none of the scars a cut citation leaves behind."""
    t = " ".join((text or "").split())
    if not t or not re.search(r"[A-Za-z]", t):
        return False
    core = t.rstrip("\"'`)]}”’")
    if not core or core[-1] not in ".!?":
        return False
    if t.count("(") != t.count(")") or t.count("[") != t.count("]"):
        return False
    if t.count("{") != t.count("}") or t.count("`") % 2:
        return False
    if any(s.search(t) for s in SCARS):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z.'’-]*", core)
    if words:
        last = words[-1].lower().strip(".-'’")
        prev = words[-2].lower().strip(".-'’") if len(words) > 1 else ""
        if last in TERMINAL_ABBREV:
            return False
        if last in DANGLING and not (last in PREPOSITIONS and _verbish(prev)):
            return False
    lead = re.match(r"[A-Za-z][A-Za-z'’-]*", t)
    if lead and lead.group(0).lower() in LEAD_ORPHAN:
        return False
    return True


def _unbreak(text: str) -> str:
    """Rejoin a compound the author hyphen-broke across lines, so that
    `unit-\\ntestable` comes back as `unit-testable`, not `unit- testable`."""
    return re.sub(r"([a-z])-[ \t]*\n[ \t]*#?[ \t]*([a-z])", r"\1-\2", text)


def _tidy(s: str) -> str:
    s = " ".join(s.split())
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    s = re.sub(r"([,;:])\s*([,;:])", r"\2", s)
    return s.strip()


def _tidy_group(inner: str) -> str:
    """What is left of one parenthetical after its citations go. Empty means
    the whole parenthesis goes with them."""
    s = _tidy(CIT.sub("", inner))
    s = re.sub(r"^[,;:]\s*", "", s)
    s = re.sub(r"\s*[,;:]\s*$", "", s)
    s = re.sub(r",\s*,", ",", s)
    return _tidy(s)


def strip_citations(text: str) -> str:
    """Drop seq citations, dates and reviewer names — parenthesis and all when
    that is all the parenthesis held."""
    out, stack = [], []
    for ch in _unbreak(text):
        if ch == "(":
            stack.append(len(out))
            out.append(ch)
        elif ch == ")" and stack:
            start = stack.pop()
            raw = "".join(out[start + 1:])
            if not raw.strip():
                # `_run()` is code, not an emptied citation. Leave it alone.
                out.append(ch)
                continue
            inner = _tidy_group(raw)
            del out[start:]
            if inner:
                out.append("(" + inner + ")")
            elif out and out[-1].endswith(" "):
                out[-1] = out[-1].rstrip() or " "
        else:
            out.append(ch)
    return _tidy(CIT.sub("", "".join(out)))


def first_sentence(text: str, limit: int = MAX_SENT) -> str:
    """The first whole sentence of the first paragraph, or nothing."""
    para = strip_citations(text.strip().split("\n\n", 1)[0])
    for sent in split_sentences(para)[:1]:
        sent = re.sub(r"[\s,;:]+$", "", sent)
        if sent and sent[-1] not in ".!?":
            sent += "."
        if len(sent) <= limit and acceptable(sent):
            return sent
    return ""


def lead_sentences(text: str, limit: int, count: int = 4) -> str:
    """The first paragraph's whole sentences, up to `limit` characters. Used
    where one sentence is a promise the rest of the block was keeping — a
    section banner's heading, or a docstring a `see docstring` points at."""
    para = strip_citations(text.strip().split("\n\n", 1)[0])
    kept = []
    for sent in split_sentences(para)[:count]:
        if not acceptable(sent):
            break
        if kept and len(" ".join(kept)) + 1 + len(sent) > limit:
            break
        kept.append(sent)
        if len(" ".join(kept)) > limit:
            break
    if len(kept) > 1 and len(" ".join(kept)) > limit:
        kept.pop()
    return " ".join(kept)


# ---------------------------------------------------------------------------
# Docstrings.
# ---------------------------------------------------------------------------

def _wrap_doc(indent: str, text: str) -> str:
    if text.endswith('"'):
        text += " "
    if len(indent) + len(text) + 6 <= 88:
        return f'{indent}"""{text}"""\n'
    words, lines, cur = text.split(), [], indent + '"""'
    for w in words:
        add = w if cur.endswith('"""') else " " + w
        if len(cur) + len(add) > 84 and not cur.endswith('"""'):
            lines.append(cur)
            cur = indent + w
        else:
            cur += add
    lines.append(cur + '"""')
    return "\n".join(lines) + "\n"


def _points_at_docstring(src_lines, node) -> bool:
    """A `# noqa: ... — see docstring` in the body is a pointer; the docstring
    it points at has to still say the thing."""
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start)
    body = "".join(src_lines[start - 1:end])
    return bool(re.search(r"see (?:the )?docstring", body, re.I))


def rewrite_docstrings(src: str) -> str:
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (not body or not isinstance(body[0], ast.Expr)
                or not isinstance(getattr(body[0], "value", None), ast.Constant)
                or not isinstance(body[0].value.value, str)):
            continue
        d = body[0]
        raw = d.value.value
        if isinstance(node, ast.Module):
            new = lead_sentences(raw, 10_000, count=3)
        elif _points_at_docstring(lines, node):
            new = lead_sentences(raw, MAX_DOC * 2)
        else:
            new = first_sentence(raw, MAX_DOC)
        if not new:
            # A docstring cannot simply go — the node would too. Keep the whole
            # first paragraph rather than emit a fragment; keep the original if
            # even that will not pass the gate.
            new = lead_sentences(raw, 10_000, count=8)
        if not new:
            continue
        new = new.replace('"""', "'''").replace("\\", "\\\\")
        indent = re.match(r"\s*", lines[d.lineno - 1]).group(0)
        edits.append((d.lineno, d.end_lineno, _wrap_doc(indent, new)))
    for start, end, text in sorted(edits, reverse=True):
        lines[start - 1:end] = [text]
    return "".join(lines)


# ---------------------------------------------------------------------------
# Comments.
# ---------------------------------------------------------------------------

def _paragraphs(block) -> list:
    """A comment block's paragraphs; a bare `#` line separates them."""
    paras, cur = [], []
    for line in block:
        body = line.lstrip("#").strip()
        if body:
            cur.append(body)
        elif cur:
            paras.append("\n".join(cur))
            cur = []
    if cur:
        paras.append("\n".join(cur))
    return paras


def _wrap_comment(indent: str, text: str) -> list:
    words, out, cur = text.split(), [], indent + "#"
    for w in words:
        if len(cur) + 1 + len(w) > 84:
            out.append(cur + "\n")
            cur = indent + "# " + w
        else:
            cur += " " + w
    out.append(cur + "\n")
    return out


def _clean_rule_line(line: str) -> str:
    """A banner's own line: citations out, the frame left alone."""
    if not CIT.search(line):
        return line
    new = _tidy(CIT.sub("", line.rstrip("\n")))
    new = re.sub(r"\(\s*\)", "", new).rstrip()
    return (new if re.search(r"\w", new) else line.rstrip("\n")) + "\n"


def _gather(lines, full, i, n):
    """The run of plain full-line comments starting at line index i."""
    block, j = [], i
    while (j < n and (j + 1) in full
           and not KEEP_DIRECTIVE.search(full[j + 1])
           and not BANNER.match(lines[j])):
        block.append(full[j + 1])
        j += 1
    return block, j


def _render_block(indent, block, banner: bool):
    paras = _paragraphs(block)
    if not paras:
        return []
    if banner:
        sent = lead_sentences(paras[0], MAX_PARA)
        if not acceptable(sent):
            return []
    else:
        sent = first_sentence(paras[0], MAX_SENT)
        if len(sent) < MIN_COMMENT or re.match(r"^[\W\d]*$", sent):
            return []
    return _wrap_comment(indent, sent)


def rewrite_comments(src: str) -> str:
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    lines = src.splitlines(keepends=True)
    full, trail = {}, {}
    for t in toks:
        if t.type == tokenize.COMMENT:
            if lines[t.start[0] - 1].strip().startswith("#"):
                full[t.start[0]] = t.string
            else:
                trail[t.start[0]] = t
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = i + 1
        if ln in full and not KEEP_DIRECTIVE.search(full[ln]):
            indent = re.match(r"\s*", lines[i]).group(0)
            if RULE.match(lines[i]):
                block, j = _gather(lines, full, i + 1, n)
                closed = j < n and (j + 1) in full and RULE.match(lines[j])
                body = _render_block(indent, block, banner=True)
                if closed:
                    # A frame with nothing in it is not a heading. Drop it all.
                    if body:
                        out.append(_clean_rule_line(lines[i]))
                        out.extend(body)
                        out.append(_clean_rule_line(lines[j]))
                    i = j + 1
                    continue
                out.append(_clean_rule_line(lines[i]))
                out.extend(body)
                i = j
                continue
            if BANNER.match(lines[i]):
                out.append(_clean_rule_line(lines[i]))
                block, j = _gather(lines, full, i + 1, n)
                out.extend(_render_block(indent, block, banner=True))
                i = max(j, i + 1)
                continue
            block, j = _gather(lines, full, i, n)
            out.extend(_render_block(indent, block, banner=False))
            i = j
            continue
        if ln in trail:
            t = trail[ln]
            if not KEEP_DIRECTIVE.search(t.string) and (
                    CIT.search(t.string) or len(t.string) > 50):
                out.append(lines[i][: t.start[1]].rstrip() + "\n")
                i += 1
                continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def collapse_blank_runs(src: str) -> str:
    return re.sub(r"\n{4,}", "\n\n\n", src)


def strip_doc_values(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            b = node.body
            if (b and isinstance(b[0], ast.Expr)
                    and isinstance(getattr(b[0], "value", None), ast.Constant)
                    and isinstance(b[0].value.value, str)):
                b[0].value.value = ""
    return tree


def prove(before: str, after: str) -> bool:
    a = ast.dump(strip_doc_values(ast.parse(before)))
    b = ast.dump(strip_doc_values(ast.parse(after)))
    return a == b


def sweep(before: str) -> str:
    return collapse_blank_runs(rewrite_comments(rewrite_docstrings(before)))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--prove"]:
        ok = prove(open(sys.argv[2]).read(), open(sys.argv[3]).read())
        print("AST equal minus docstrings" if ok else "AST DIFFERS")
        raise SystemExit(0 if ok else 1)
    path = sys.argv[1]
    before = open(path).read()
    after = sweep(before)
    assert prove(before, after), "AST changed!"
    open(sys.argv[2], "w").write(after)
    print(f"{len(before)} -> {len(after)} bytes ({100*len(after)/len(before):.0f}%)")
