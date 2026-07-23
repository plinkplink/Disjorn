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

# 2. Mark 50-genesis resident-only. It is biography and the seat end-state
#    keeps it OUT of the build seat; the seat mechanism reads this from
#    frontmatter, so the one-line addition below is REQUIRED for the build
#    seat to skip genesis. (10/20/30/40 need NO change — absent `seats:`
#    already means both seats, which is what the operational set wants.)
#
#    Edit 50-genesis.md frontmatter from:
#        ---
#        name: genesis
#        ---
#    to:
#        ---
#        name: genesis
#        seats: [resident]
#        ---

git add 00-nonnegotiables.md 05-bearings.md 50-genesis.md
git commit -m "spine: split 00-kernel -> 00-nonnegotiables (both seats) + 05-bearings (resident); genesis resident-only"
```

## Verify the split (before publishing the mirror)

The seat loader lives in `harness/house_memory`. A quick check that the split
spine assembles the intended sets (run from the Disjorn repo, with the venv):

```sh
/home/plink/Disjorn/Disjorn/server/.venv/bin/python - <<'PY'
from house_memory import Spine
s = Spine("/home/plink/bots/fable/spine")
res = [e.name for e in s.entries_for_seat("resident")]
bld = [e.name for e in s.entries_for_seat("build")]
print("resident:", res)   # expect all entries incl. bearings, genesis
print("build:   ", bld)   # expect nonnegotiables, people, load-bearing-walls,
                          #        build-rhythm, cautions — NO bearings, NO genesis
assert "bearings" not in bld and "genesis" not in bld
assert s.assemble_for_seat("resident") == s.load_kernel()  # zero-regression
print("OK")
PY
```

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
