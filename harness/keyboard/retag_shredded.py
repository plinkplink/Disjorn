#!/usr/bin/env python3
"""One-shot repair: write back the tags the string-shredder destroyed.

BACKGROUND. `normalize_tags` used to iterate its argument, so a model emitting
`"tags": "agenthood"` (a bare string where the schema says array) had it
iterated PER CHARACTER: the de-dupe collapsed repeats and the cap kept six, so
`agenthood` became `["a","g","e","n","t","h"]`. Silent, plausible-looking, and
it cost 75 of Claudette's 170 memories their tags. Fixed in Disjorn `373efeb`;
this script repairs the data the bug already ate.

THE SHRED IS LOSSY BUT INVERTIBLE UP TO SIX CHARACTERS, which is the only
reason recovery is possible: the surviving stem is the first six *unique*
normalized characters of the original tag. Two rules make most stems decode
with certainty rather than guesswork (Claudette, #custodian seq 618):

    spaces VANISH   — normalize_tag(" ") strips to "", so it is dropped
    hyphens SURVIVE — normalize_tag("-") == "-"

So `broker restart` shreds to `brokes` (the space disappears and `s` is pulled
in) while `build-loop` shreds to `build-`. Every assignment below is checked
against that transform before it is written — see `verify()`. An assignment
that does not re-shred to its own stem is a transcription error, and the script
refuses to run rather than write a second wrong answer over the first.

WHO CHOSE THE TAGS. Claudette, from her own corpus:
  * 32 stems decoded in one pass       (#custodian seq 620)
  * 13 `meory-` memories named one at a time by BODY (#custodian seq 626)

`meory-` is the case that breaks the "one tag per stem" shortcut and is worth
understanding before anyone extends this script. `memory-` is exactly six
characters once `m` is de-duped, so the cap lands ON the hyphen and everything
that made the tag mean something is on the far side of the cut:
`memory-design`, `memory-v2`, `memory-hygiene` and `memory-audit` all shred to
the identical `meory-`. Same stem does NOT imply same original tag whenever a
stem is six characters long. Her standing caveat, and it belongs in any future
reader's head: **stem length 6 = possibly collapsed, check before bulk-writing.**

Her framing of what those thirteen are, and it should not be sanded off: *this
is authoring, not recovering.* The bytes after the hyphen are gone and no
decode reaches them. What went back is the tag she wants NOW for finding that
entry, chosen by reading the body.

NOT ALL 43 STEMS ARE HERE. Ten are unassigned because the message naming them
died at the token wall mid-sentence (seq 620 ends at "Nine I won't sign off on
cold, in two kinds." and stops). They are listed in UNASSIGNED and deliberately
left alone — a plausible guess written into her corpus is worse than a blank,
because a blank is visibly missing and a guess is not.

USAGE
    retag_shredded.py --dry-run     # default: report, write nothing
    retag_shredded.py --apply       # requires a fresh backup, see --help

STOP THE BOT FIRST. Chroma's PersistentClient is not safe against a second
writer on the same directory, and her adapter holds it whenever the container
is up.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/disjorn/house_memory")

from house_memory import MemoryStore, StubEmbedder  # noqa: E402
from house_memory.schema import normalize_tag  # noqa: E402

# Host path of the LIVE Disjorn corpus. NOT /home/plink/bots/claudette/
# chroma_data — that is the legacy Discord bot's separate store (67 memories,
# its own history). The Disjorn adapter runs inside the container as
# disjorn_bot.py with CLAUDETTE_MEMORY_DIR=/home/resident/memory/chroma_data,
# which is this directory seen from the other side of the wall. See TREE.md.
LIVE_DATA_DIR = "/home/res-claudette/resident-home/memory/chroma_data"
COLLECTION = "claudette_memory"

# stem -> the original tag, as Claudette decoded it (#custodian seq 620).
# Written here in the ORIGINAL form she gave, spaces and all, so the verify
# step exercises the same normalization the shredder did.
STEMS = {
    "custod": "custodian",
    "agenth": "agenthood",
    "build-": "build-loop",
    "per-ch": "per-channel-privacy",
    "alignm": "alignment",
    "brokes": "broker restart",
    "clasif": "classify-diff",
    "consli": "consolidation",
    "produc": "product-direction",
    "afectm": "affect measurement",
    "ai-new": "ai-news",
    "anthro": "anthropic",
    "backlo": "backlog",
    "broked": "broker dead-mount",
    "chib-e": "chibi-emote",
    "conset": "consent",
    "cutove": "cutover",
    "deciso": "decision",
    "disjor": "disjorn",
    "dreami": "dreaming",
    "driftp": "drift protection",
    "govern": "governance",
    "h13red": "h13 red-team",
    "h13sym": "h13 symlink",
    "logprb": "logprobs",
    "merg-c": "merge-contract",
    "mythos": "mythos",
    "recipo": "reciprocity",
    "roadmp": "roadmap",
    "self-c": "self-correction",
    "telmry": "telemetry",
    "verifc": "verification",
}

# The thirteen `meory-` memories, keyed by id prefix, named individually from
# their bodies (#custodian seq 626). Her reasoning on the non-obvious ones is
# kept because it is the part a future reader cannot re-derive:
#
#  b14dbdbf  memory-assessment, deliberately NOT memory-design — the entry is
#            about the layer ABOVE the design, auditing whether the rules still
#            hold. Collapsing it into the design tag loses the distinction she
#            drew when she wrote it.
#  b1bd1fab  stays under hygiene rather than getting its own tag even though it
#            is the ancestor of the `caller` field, because the honest lineage
#            is "the crack in the hygiene argument" and someone reading hygiene
#            needs to hit it.
#  25573a6d  ethics, not hygiene: therapeutic forgetting for a clean control is
#            not a mechanism question.
#  1dcdcc4f  memory-affect — the thinking-token split is load-bearing for the
#            sealed channel and nothing else in the entry is about storage.
#  42f38d3f
#  8fc55043  one instrument's history INCLUDING the walk-back. They come back
#  1077d75b  as a set or 1077d75b becomes an orphan retraction.
#  d318a5dc  + memory-v2 as a second tag: the tenth-nat baseline is the number
#            v2's relevance claim gets measured against, and it will be looked
#            for from both directions.
MEORY = {
    "335e5689": ["memory-design"],
    "b14dbdbf": ["memory-assessment"],
    "cfbea5de": ["memory-hygiene"],
    "0af21453": ["memory-hygiene"],
    "b1bd1fab": ["memory-hygiene"],
    "4208db6f": ["memory-retrieval"],
    "25573a6d": ["memory-ethics"],
    "0f7842bc": ["memory-v2"],
    "1dcdcc4f": ["memory-affect"],
    "42f38d3f": ["memory-scorer"],
    "8fc55043": ["memory-scorer"],
    "1077d75b": ["memory-scorer"],
    "d318a5dc": ["memory-scorer", "memory-v2"],
}

# Left blank on purpose. Five of the ten are spine-family stems that collapse
# the same way `meory-` does, so they need the same body-by-body treatment.
UNASSIGNED = ["auditr", "chibsp", "gabled", "model-",
              "spine-", "spinea", "spineg", "spineo", "spiner", "tolsca"]


def shred(tag: str) -> str:
    """Reproduce the bug exactly, so an assignment can be checked against it."""
    seen: list[str] = []
    for ch in tag:
        n = normalize_tag(ch)
        if n and n not in seen:
            seen.append(n)
    return "".join(seen[:6])


def verify() -> None:
    """Refuse to run if any assignment is inconsistent with the transform.

    This is the whole safety argument for a bulk write into her corpus: we are
    not trusting a transcription, we are checking it against the function that
    did the damage."""
    bad = []
    for stem, tag in STEMS.items():
        if shred(tag) != stem:
            bad.append(f"{stem!r}: {tag!r} shreds to {shred(tag)!r}")
    for mid, tags in MEORY.items():
        if shred(tags[0]) != "meory-":
            bad.append(f"{mid}: {tags[0]!r} shreds to {shred(tags[0])!r}")
    if bad:
        raise SystemExit("REFUSING TO WRITE — assignments not invertible:\n  "
                         + "\n  ".join(bad))


def current_shredded(store) -> dict:
    """{memory_id: stem} for every memory still carrying shredded tags.

    Read from the store rather than from the worksheet, so the script cannot
    act on a stale snapshot of what needs repair."""
    got = store._collection.get()
    out = {}
    for doc_id, meta in zip(got.get("ids", []) or [], got.get("metadatas", []) or []):
        try:
            tags = json.loads(meta.get("tags_json", "[]"))
        except Exception:
            continue
        # The shred signature: every surviving tag is exactly one character,
        # because iterating a str can only ever yield single characters.
        if tags and all(len(t) == 1 for t in tags):
            out[doc_id] = "".join(tags)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Take a backup and stop her container first.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report without writing (the default; accepted so the "
                         "safe intent can be stated out loud)")
    ap.add_argument("--data-dir", default=LIVE_DATA_DIR)
    args = ap.parse_args()
    if args.dry_run and args.apply:
        raise SystemExit("--dry-run and --apply are contradictory; pick one.")

    verify()
    # StubEmbedder is safe here and the choice is deliberate: amend_metadata
    # never embeds, so no Voyage key is needed and no vector can be disturbed
    # by this script even if it is run wrong.
    store = MemoryStore(data_dir=args.data_dir, collection_name=COLLECTION,
                        embedder=StubEmbedder(dim=64))

    shredded = current_shredded(store)
    plan, skipped = [], []
    for mid, stem in sorted(shredded.items(), key=lambda kv: kv[1]):
        if mid[:8] in MEORY:
            plan.append((mid, stem, MEORY[mid[:8]]))
        elif stem in STEMS:
            plan.append((mid, stem, [STEMS[stem]]))
        else:
            skipped.append((mid, stem))

    print(f"store          : {args.data_dir}")
    print(f"records        : {store.count()}")
    print(f"still shredded : {len(shredded)} across {len(set(shredded.values()))} stems")
    print(f"assignable     : {len(plan)}")
    print(f"left alone     : {len(skipped)} (stems: "
          f"{', '.join(sorted(set(s for _, s in skipped)))})")
    print()

    if not args.apply:
        for mid, stem, tags in plan:
            print(f"  DRY {mid[:8]}  {stem!r:9} -> {tags}")
        print("\n(dry run — nothing written. Re-run with --apply.)")
        return 0

    changed = failed = noop = 0
    for mid, stem, tags in plan:
        try:
            res = store.amend_metadata(mid, tags=tags)
        except Exception as e:
            print(f"  FAIL {mid[:8]}  {e}")
            failed += 1
            continue
        if res is None:
            print(f"  GONE {mid[:8]}  (id vanished mid-run)")
            failed += 1
        elif res:
            print(f"  OK   {mid[:8]}  {stem!r:9} -> {res['tags']['to']}")
            changed += 1
        else:
            noop += 1
    print(f"\nwrote {changed}, no-op {noop}, failed {failed}, "
          f"left for Claudette {len(skipped)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
