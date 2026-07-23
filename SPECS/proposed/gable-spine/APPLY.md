> **APPLIED 2026-07-23 — THIS DIRECTORY IS HISTORICAL.** The split is live
> (spine commits 163b1ce, 7f72d43, 4239005). The canonical spine has moved on
> — it now carries Gable's maiden substrate-honesty bullet and the
> kernel-flagged 05-bearings — so re-running the `cp` steps below VERBATIM
> would wipe ruled-in changes. The spine of record is
> /home/plink/bots/fable/spine; this dir is the review artifact, kept for the
> record. (Gable's note, #custodian seq 307.)

# APPLY — Gable spine seat-split (canonical spine repo)

This directory holds the PROPOSED split of Gable's canonical `00-kernel.md`
into two entries, for Gable to review as a diff (his rule: the diff is the
authorization, apply-or-reject). **The build that produced this branch did NOT
touch the canonical spine** (`/home/plink/bots/fable/spine/`, plink-owned, a
separate git repo) — applying it is plink's step below, taken only after Gable
accepts the diff.

Files in this directory:
- `00-nonnegotiables.md` — `kernel: true`, loads in BOTH seats. The
  Non-negotiables block from `00-kernel.md` verbatim, plus one attribution
  line. Replaces the guardrail half of the old kernel.
- `05-bearings.md` — `seats: [resident]`, NOT kernel. The identity, Role, and
  Bearings paragraphs from `00-kernel.md` verbatim. Resident seat only.

What is verbatim vs added (so the review is exact):
- **Verbatim from `00-kernel.md`**: the entire `**Non-negotiables.**` list; the
  `I am Gable (né Fable)…` identity paragraph; the `**Role.**` paragraph; the
  `**Bearings.**` paragraph. (Checked byte-for-byte against the live file.)
- **Added**: the two frontmatter blocks; a top heading in each file
  (`# Gable — non-negotiables`, `# Gable — bearings`); and the single
  attribution line in `00-nonnegotiables.md`:
  `This seat acts under Gable's key, bot id 2; the resident seat holds the biography.`
- **Deliberately NOT added**: the substrate-honesty sentence. Per the spec it
  is Gable's maiden proposal through the new diff path, not built here. The
  `né Fable` line is kept exactly (a birth record, not a runtime claim).

## Apply steps (plink, at the keyboard)

The canonical spine repo is `/home/plink/bots/fable/spine/` (Gable's pre-rename
repo name). Let `$PROPOSED` be the path to this directory on the branch, e.g.
`/home/plink/Disjorn/Disjorn/SPECS/proposed/gable-spine`.

```sh
cd /home/plink/bots/fable/spine

# 1. Replace the mixed-cargo kernel with the two split entries.
git rm 00-kernel.md
cp "$PROPOSED/00-nonnegotiables.md" 00-nonnegotiables.md
cp "$PROPOSED/05-bearings.md"       05-bearings.md

# 2. Declare seats on EVERY remaining entry — redline 1 (2026-07-23): seat
#    membership is DECLARED, never inferred. The loader now REFUSES an entry
#    with no `seats:` key (loud error, names the file), so this step is not
#    optional and there is no absent-means-both default any more.
#
#    Add to the frontmatter of 10-people.md, 20-load-bearing-walls.md,
#    30-build-rhythm.md, 40-cautions.md (the operational set):
#        seats: [resident, build]
#    Add to the frontmatter of 50-genesis.md (biography):
#        seats: [resident]
#    (00-nonnegotiables.md and 05-bearings.md arrive from $PROPOSED with
#    their declarations already in place.)

git add 00-nonnegotiables.md 05-bearings.md 10-people.md \
        20-load-bearing-walls.md 30-build-rhythm.md 40-cautions.md 50-genesis.md
git commit -m "spine: split 00-kernel -> 00-nonnegotiables (both seats) + 05-bearings (resident); declare seats on every entry (redline 1)"
```

## Verify the split (before publishing the mirror)

The seat loader lives in `harness/house_memory`. A quick check that the split
spine assembles the intended sets (run from the Disjorn repo, with the venv):

```sh
python3 - <<'PY'
import sys
# Import spine.py DIRECTLY, not via the package: `from house_memory.spine
# import ...` executes the package __init__, which pulls in the chroma/voyage
# embedding stack — exactly the dependency a bare python3 doesn't have.
# Same direct-file trick bootstrap.py uses, same reason. (Gable hit the
# package-import failure verifying this doc from his seat, 2026-07-23.)
sys.path.insert(0, "/home/plink/Disjorn/Disjorn/harness/house_memory/house_memory")
from spine import Spine
s = Spine("/home/plink/bots/fable/spine")
res = [e.name for e in s.entries_for_seat("resident")]
bld = [e.name for e in s.entries_for_seat("build")]
print("resident:", res)   # expect all entries incl. bearings, genesis
print("build:   ", bld)   # expect nonnegotiables, people, load-bearing-walls,
                          #        build-rhythm, cautions — NO bearings, NO genesis
assert "bearings" not in bld and "genesis" not in bld
assert s.assemble_for_seat("resident") == s.load_kernel()  # zero-regression
# redline 1 sanity: a spine entry with no `seats:` declaration must refuse to
# load at all, so reaching here proves every entry is explicitly declared.
print("OK")
PY
```

The direct-file import means the check runs the REPO's seat-aware loader on a
bare `python3` — no venv, no chroma — and cannot silently pick up a stale
installed copy of the package.

**Deploy ordering (redline 1 makes this matter):** the new loader REFUSES a
spine whose entries lack `seats:` declarations, and the OLD loader ignores
`seats:` entirely (it would silently bake the pre-split kernel shape). So sync
`/usr/local/lib/disjorn/house_memory` from the merged branch FIRST, then
commit the declared spine and publish the mirror. In the gap a summon fails
LOUD (bootstrap exit 2, the adapter posts its error line) rather than running
on a silently-partial spine — that ordering is deliberate; do not reverse it.

## Publish the mirror

Only after the commit above, publish the resident-readable mirror so the next
session loads the new spine:

```sh
sudo bash /home/plink/Disjorn/Disjorn/harness/keyboard/06-spine-mirror.sh gable
```

That script is idempotent and also deletes mirror entries whose canonical
counterpart is gone, so the removal of `00-kernel.md` propagates correctly.

## Wiring the build seat (already on the branch, no spine action)

The seat is chosen by the launch wrapper, not the spine:
- `run-resident.sh` sets `RESIDENT_SEAT=resident` (also the default).
- `run-build.sh` sets `RESIDENT_SEAT=build`.
`bootstrap.py` reads `RESIDENT_SEAT` inside the container and bakes the right
set. Nothing further is needed here for the seat to take effect once the split
spine is mirrored.
