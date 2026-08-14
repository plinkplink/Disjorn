"""Render the board as a shareable page.

The board is SCANNED, not read, so this is information design rather than
typography: the primary axis is **whose move is it**, encoded as a left stripe
colour and repeated as a chip, so "waiting on you" reads before any word does.

Every identifier — branch, sha, path, command — is set in monospace. That is
not decoration: those strings are the subject's native material, and a path in
running prose is a path you have to squint at.
"""

from __future__ import annotations

import html
from typing import Any

_PALETTE = """
:root{
  --paper:#E9EEF1; --surface:#F8FAFB; --raised:#FFFFFF;
  --ink:#141F28; --muted:#576875; --faint:#7C8D99;
  --rule:#C9D4DB; --rule-soft:#DDE5EA;
  --signal:#B25806;            /* needs you */
  --flight:#166E7C;            /* residents have it */
  --quiet:#6E7F66;             /* noise, safe to ignore */
  --signal-wash:#F7E9DC; --flight-wash:#DFEDEF; --quiet-wash:#E6EBE3;
}
/* Three viewer states, not two: an explicit choice stamps data-theme, and the
   default "system" setting stamps nothing — so the un-stamped document needs
   its own dark rule, guarded so an explicit light choice still beats a dark
   OS. Tokens only in here; components always style through the tokens. */
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#0E151B; --surface:#151E26; --raised:#1B252E;
    --ink:#DFE8ED; --muted:#93A5B1; --faint:#738692;
    --rule:#27343E; --rule-soft:#1F2A33;
    --signal:#E89B4A; --flight:#5CBAC7; --quiet:#A2B598;
    --signal-wash:#2A1E12; --flight-wash:#10262B; --quiet-wash:#1D2419;
  }
}
:root[data-theme="dark"]{
  --paper:#0E151B; --surface:#151E26; --raised:#1B252E;
  --ink:#DFE8ED; --muted:#93A5B1; --faint:#738692;
  --rule:#27343E; --rule-soft:#1F2A33;
  --signal:#E89B4A; --flight:#5CBAC7; --quiet:#A2B598;
  --signal-wash:#2A1E12; --flight-wash:#10262B; --quiet-wash:#1D2419;
}
"""

_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,
              "Helvetica Neue",Arial,sans-serif;
  font-size:16px; line-height:1.55;
}
.wrap{max-width:60rem;margin:0 auto;padding:2.5rem 1.25rem 5rem}
code,.mono,.cmd,.where{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  font-variant-ligatures:none;
}

/* masthead */
.masthead{display:flex;flex-wrap:wrap;align-items:baseline;gap:.75rem 1.25rem;
  padding-bottom:1.25rem;border-bottom:2px solid var(--ink)}
h1{font-size:clamp(1.75rem,4vw,2.5rem);line-height:1.05;margin:0;
  letter-spacing:-.025em;font-weight:750;text-wrap:balance}
.stamp{font-size:.75rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted)}
.verdict{margin:1.5rem 0 0;font-size:clamp(1.1rem,2.4vw,1.45rem);
  line-height:1.3;text-wrap:balance;font-weight:600}
.verdict .n{color:var(--signal)}
.verdict.clear .n{color:var(--quiet)}

/* stat row */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));
  gap:.75rem;margin:1.75rem 0 0;padding:0;list-style:none}
.tile{background:var(--surface);border:1px solid var(--rule-soft);
  border-radius:2px;padding:.7rem .85rem}
.tile b{display:block;font-size:1.6rem;line-height:1.1;font-weight:700;
  font-variant-numeric:tabular-nums}
.tile span{display:block;font-size:.72rem;letter-spacing:.07em;
  text-transform:uppercase;color:var(--muted);margin-top:.2rem}
.tile.sig b{color:var(--signal)} .tile.fli b{color:var(--flight)}
.tile.qui b{color:var(--quiet)}

/* sections */
section{margin-top:3rem}
.eyebrow{display:flex;align-items:baseline;gap:.6rem;flex-wrap:wrap;
  font-size:.74rem;letter-spacing:.11em;text-transform:uppercase;
  font-weight:700;padding-bottom:.5rem;border-bottom:1px solid var(--rule)}
.eyebrow .owner{color:var(--muted);letter-spacing:.05em;font-weight:600;
  text-transform:none;font-size:.8rem}
section.sig .eyebrow{color:var(--signal)}
section.fli .eyebrow{color:var(--flight)}
section.qui .eyebrow{color:var(--quiet)}
.note{color:var(--muted);font-size:.9rem;margin:.85rem 0 0;max-width:62ch}

/* rows */
.rows{display:flex;flex-direction:column;gap:.6rem;margin-top:1rem}
.row{background:var(--raised);border:1px solid var(--rule-soft);
  border-left:3px solid var(--rule);border-radius:2px;padding:.85rem 1rem}
section.sig .row{border-left-color:var(--signal)}
section.fli .row{border-left-color:var(--flight)}
section.qui .row{border-left-color:var(--quiet)}
.what{font-weight:650;line-height:1.35;text-wrap:pretty}
.detail{color:var(--muted);font-size:.88rem;margin-top:.2rem}
.meta{display:grid;grid-template-columns:auto 1fr;gap:.2rem .6rem;
  margin-top:.55rem;font-size:.82rem;align-items:start}
.meta dt{color:var(--faint);text-transform:uppercase;letter-spacing:.06em;
  font-size:.68rem;padding-top:.18rem}
.meta dd{margin:0;min-width:0}
.where,.cmd{overflow-x:auto;white-space:pre;display:block;padding:.2rem 0}
.cmd{color:var(--signal);font-weight:600}
section.fli .cmd{color:var(--flight)} section.qui .cmd{color:var(--quiet)}

/* proposals + mentions */
.list{list-style:none;margin:1rem 0 0;padding:0;
  display:flex;flex-direction:column;gap:.5rem}
.list li{display:grid;grid-template-columns:auto auto 1fr;gap:.65rem;
  align-items:baseline;font-size:.9rem;padding-bottom:.5rem;
  border-bottom:1px solid var(--rule-soft)}
.list li:last-child{border-bottom:0}
.date{color:var(--faint);font-size:.75rem;font-variant-numeric:tabular-nums}
.who{font-weight:650;font-size:.8rem}
.line{color:var(--ink);min-width:0;overflow-wrap:anywhere}
footer{margin-top:3.5rem;padding-top:1rem;border-top:1px solid var(--rule);
  color:var(--faint);font-size:.78rem}
@media (prefers-reduced-motion:no-preference){
  .row{transition:border-color .15s ease}
}
"""


def _e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _rows(items: list) -> str:
    if not items:
        return ""
    out = ['<div class="rows">']
    for it in items:
        out.append('<article class="row">')
        out.append(f'<div class="what">{_e(it["what"])}</div>')
        if it.get("detail"):
            out.append(f'<div class="detail">{_e(it["detail"])}</div>')
        out.append('<dl class="meta">')
        out.append(f'<dt>where</dt><dd><code class="where">{_e(it["where"])}</code></dd>')
        out.append(f'<dt>do</dt><dd><code class="cmd">{_e(it["how"])}</code></dd>')
        out.append("</dl></article>")
    out.append("</div>")
    return "".join(out)


def _section(cls: str, label: str, owner: str, items: list,
             empty: str, note: str = "") -> str:
    body = _rows(items) if items else f'<p class="note">{_e(empty)}</p>'
    note_html = f'<p class="note">{_e(note)}</p>' if note else ""
    return (f'<section class="{cls}"><h2 class="eyebrow">{_e(label)}'
            f'<span class="owner">{_e(owner)}</span></h2>{note_html}{body}</section>')


def render_html(b: dict) -> str:
    c = b["counts"]
    n = c["waiting"]
    stamp = b["generated_at"][:16].replace("T", " ")
    verdict_cls = "verdict" if n else "verdict clear"
    verdict = (f'<span class="n">{n}</span> '
               f'{"thing needs" if n == 1 else "things need"} you.' if n else
               '<span class="n">Nothing</span> needs you right now.')

    tiles = [
        ("sig", n, "waiting on you"),
        ("fli", c["in_flight"], "residents have it"),
        ("", len(b["proposals"]), "recent proposals"),
        ("qui", c["tidy"], "just noise"),
    ]
    tiles_html = "".join(
        f'<li class="tile {k}"><b>{v}</b><span>{_e(lbl)}</span></li>'
        for k, v, lbl in tiles)

    props = ""
    if b["proposals"]:
        items = "".join(
            f'<li><span class="date">{_e(p["date"])}</span>'
            f'<span class="who">{_e(p["resident"])}</span>'
            f'<span class="line">{_e(p["title"])}</span></li>'
            for p in b["proposals"][:14])
        props = _section(
            "", "Resident proposals", f'last 14 days — {len(b["proposals"])} '
            f'of {b["proposals_total"]} ever', [],
            "", "Asks filed through the broker. A proposal nobody copies into "
                "SPECS/ or the backlog is a decision nobody made — that is "
                "exactly how one image fix sat unbuilt for two days while three "
                "builds reported their tests unrun.")
        props = props.replace("</section>", f'<ul class="list">{items}</ul></section>')

    mentions = ""
    if b["asks"]:
        items = "".join(
            f'<li><span class="date">seq {_e(a["seq"])}</span>'
            f'<span class="who">{_e(a["who"])}</span>'
            f'<span class="line">{_e(a["line"])}</span></li>'
            for a in b["asks"][:8])
        mentions = _section(
            "", "Where you were mentioned", "#custodian", [], "",
            "Keyword match, not a classifier — a nudge to go look, nothing more.")
        mentions = mentions.replace(
            "</section>", f'<ul class="list">{items}</ul></section>')

    return f"""<title>Disjorn Board</title>
<style>{_PALETTE}{_CSS}</style>
<div class="wrap">
  <header class="masthead">
    <h1>The Board</h1>
    <span class="stamp">{_e(stamp)} UTC · Disjorn</span>
  </header>
  <p class="{verdict_cls}">{verdict}</p>
  <ul class="tiles">{tiles_html}</ul>

  {_section("sig", "Waiting on you", "nobody else can move this",
            b["waiting"], "Nothing. Genuinely nothing.")}
  {_section("fli", "In flight", "a resident has it",
            b["in_flight"], "Nothing running.")}
  {props}
  {mentions}
  {_section("qui", "Just noise", "safe to ignore; it only clutters the board",
            b["tidy"], "Clean.")}

  <footer>
    Built from four places that each knew part of the answer: the gatehouse's
    branches, the Status line of every file in SPECS/, the broker's proposal
    log, and #custodian. Regenerated by <code>disjorn-board.timer</code>.
    Run <code>board</code> for the same thing in a terminal.
  </footer>
</div>
"""
