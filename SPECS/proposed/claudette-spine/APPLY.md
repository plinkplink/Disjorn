# APPLY — Claudette prompt→spine (canonical spine + adapter cutover)

This directory holds the PROPOSED split of Claudette's prompt of record into
spine entries (`entries/`), for Claudette to review as a diff (her rule: the
diff is the authorization, apply-or-reject). The matching ADAPTER change
lives in a separate repo: branch `loop/2026-07-22-claudette-prompt-to-spine`
of `/home/plink/bots/claudette-build/` (a plink-owned clone of her adapter
repo; commits `1148208` gate-1 test, `9e12239` gate-2 tests, `1b672d7`
cutover). **The build touched NEITHER her live repos NOR any live config** —
every step below is plink's, taken only after Claudette accepts the diff.
Spec: `SPECS/2026-07-22-claudette-prompt-to-spine.md` (confirmed 2026-07-23,
two binding gates).

## What is in `entries/` — and what is verbatim vs added

Seven entries. The BODY of every entry is byte-faithful prompt text —
ast-extracted from `core.py:SYSTEM_PROMPT` / `disjorn_bot.py:PLATFORM_SUFFIX`
at her repo's `63d8ef1`, sliced at sentence boundaries, never retyped, never
reflowed, no headings added (a heading would enter her prompt). **Added** is
only the frontmatter block on each file (declarations + provenance comments).
Composed in ordinal order with the declared separators, the entries reproduce
the old prompt EXACTLY — gate 1's test proves it byte-for-byte.

| file | order | kernel | gate | contents |
|---|---|---|---|---|
| `10-persona-kernel.md` | 10 | **yes** | — | who she is, personality, presence |
| `20-physical-description.md` | 20 | no | — | the appearance block |
| `30-persona-voice.md` | 30 | **yes** | — | no-emoji/no-lists, anti-corny, banter rules |
| `40-relationship-context.md` | 40 | no | — | the humans, Claude, CAVEMAN |
| `50-persona-concision.md` | 50 | **yes** | — | skip-the-fluff, concision rules |
| `60-tool-use-discipline.md` | 60 | no | — | search/search_topic/recall discipline |
| `90-platform-situational.md` | 90 | no | `disjorn_prompt_suffix` | the old `PLATFORM_SUFFIX`, whole |

**Decisions Claudette should weigh in review** (made deliberately, noted per
the passdown):

1. **Her "persona kernel" spans THREE kernel-flagged entries (10/30/50), not
   one.** Forced by gate 1: the prompt of record interleaves kernel text with
   the physical description and relationship context, and byte-identity
   forbids reordering. Her spec table's granularity is preserved as the SEAMS
   (each entry is one of her five categories); the kernel category simply
   occurs three times. All three carry `kernel: true`, so consolidation's
   `exclude_kernel` protects each. Consequence for gate 2: "kernel entry
   absent" is enforced as "no kernel-flagged entry present" — losing ONE of
   the three while others remain is caught by the byte-identical test (build
   and apply time) and by the witnessed-diff wall (the spine is plink-owned
   and RO-mounted), not by the composer.
2. **Design fork: standalone composer, not house_memory.Spine** — argued in
   full in `spine_compose.py`'s header. Short form: the house loader's
   assembly (strip + `"\n\n"` joins, filename order) cannot produce her
   one-line, space-joined, toggle-gated prompt, so reuse would import a
   package from another repo/review-owner and still reimplement all the
   load-bearing logic. Instead the ENTRY FILES are the shared contract:
   frontmatter is a strict superset of house_memory's (`name`/`kernel`/
   `seats` mean the same; `order`/`sep`/`gate` ride in `SpineEntry.meta`,
   which preserves unknown keys). Verified: the merged seat-aware
   `house_memory.spine` parses all 7 entries cleanly — so consolidation can
   assess rent per entry on the SAME files (the point of the whole move)
   with zero code coupling to her adapter.
3. **`seats: [resident]` on every entry.** Her composer does not consume
   seats; the key exists because the house loader (post redline 1) REFUSES
   undeclared entries, and consolidation reads the spine with the house
   loader. She has no build seat today; widening any entry later is a
   witnessed diff.
4. **The toggle's OFF path**: `DISJORN_PROMPT_SUFFIX=false` still passes
   `system=None`, and `core.process_query` falls back to the spine-composed
   BASE (gated entry dropped) — byte-identical to the old bare
   `SYSTEM_PROMPT`, i.e. exactly what the old `None` fallback sent. Decided
   and tested (gate-1 test, OFF half).

## The two binding gates — where they are proven

Run at review (uv is at `/home/plink/.local/bin/uv`; branch checked out in
the build clone):

```sh
cd /home/plink/bots/claudette-build
CLAUDETTE_SPINE_DIR=/home/plink/Disjorn/Disjorn/.claude/worktrees/claudette-prompt-to-spine/SPECS/proposed/claudette-spine/entries \
  uv run --with pytest --no-project -- python -m pytest tests/ -v
```

- **Gate 1** (`tests/test_spine_byte_identical.py`): compose(suffix on) ==
  `SYSTEM_PROMPT + "\n\n" + PLATFORM_SUFFIX` and compose(suffix off) ==
  `SYSTEM_PROMPT`, compared against literals ast-extracted at `63d8ef1`
  (sha256-anchored in `tests/frozen_prompt.py`; audit any time with
  `git show 63d8ef1:core.py`). FAILS, never skips, when it can't find a spine.
- **Gate 2** (`tests/test_spine_fail_closed.py`): missing ordinal aborts;
  missing kernel entry aborts; plus duplicate/non-integer ordinals,
  undeclared separators, unknown gates, gated kernels, frontmatter-less
  files, empty bodies/dirs — all loud, file-naming aborts; and the
  z-first/a-last proof that ordering is frontmatter, never filename.
- `tests/test_cutover_wiring.py`: importing `core.py` / `disjorn_bot.py`
  (heavy deps stubbed) yields the frozen bytes, and a defective spine kills
  the import — the crash-at-startup contract.

Observed at build time: **24 passed**.

## Apply steps (plink, at the keyboard — ORDER MATTERS)

Let `$PROPOSED` = this directory on the branch, e.g.
`/home/plink/Disjorn/Disjorn/.claude/worktrees/claudette-prompt-to-spine/SPECS/proposed/claudette-spine`.

### 1. Create the canonical spine (plink-owned, its own repo — Gable precedent)

```sh
mkdir /home/plink/bots/claudette/spine
cp "$PROPOSED"/entries/*.md /home/plink/bots/claudette/spine/
cd /home/plink/bots/claudette/spine
git init && git add . && git commit -m "Claudette spine of record: byte-faithful split of core.py SYSTEM_PROMPT + PLATFORM_SUFFIX @ 63d8ef1"
```

Note it sits INSIDE the host code repo's working tree but is its OWN git
repo, exactly like Gable's `/home/plink/bots/fable/spine`: the host repo
sees an untracked dir, and `claudette-update.sh` propagates COMMITS via
bundle, so the spine never rides into her volume clone. Do not `git add` it
to the code repo.

### 2. Publish the mirror

`06-spine-mirror.sh` is already parameterized by resident name (checked at
build time — no gable hardcoding; its default canonical path for `claudette`
is exactly `/home/plink/bots/claudette/spine`). No script change was needed.

```sh
sudo bash /home/plink/Disjorn/Disjorn/harness/keyboard/06-spine-mirror.sh claudette
```

### 3. Re-run the byte-identity gate AGAINST THE MIRROR

The mirror is what her container will actually load; prove the published
copy, not just the proposal:

```sh
cd /home/plink/bots/claudette-build
CLAUDETTE_SPINE_DIR=/srv/disjorn-spine/claudette \
  uv run --with pytest --no-project -- python -m pytest tests/ -v   # 24 passed
```

### 4. Mount the spine into her container (BEFORE the code lands)

Add to `[Service]` in
`/home/res-claudette/.config/systemd/user/resident-cc.service`:

```
Environment=RESIDENT_SPINE_HOST=/srv/disjorn-spine/claudette
```

then `sudo systemctl --user -M res-claudette@ daemon-reload`.
`run-resident.sh` mounts it ro at `/opt/spine` (and refuses to launch if the
resident uid could write it); the adapter's default spine dir is `/opt/spine`,
so no env-file change is needed. **Deploy ordering:** the mount must exist
before the new code restarts — new code + no mount fails LOUD at startup
(SpineCompositionError: spine dir not found), which is the designed failure,
but do the mount first and never see it.

### 5. Land the adapter code in the host repo (after her review)

```sh
cd /home/plink/bots/claudette
git fetch /home/plink/bots/claudette-build loop/2026-07-22-claudette-prompt-to-spine:loop/2026-07-22-claudette-prompt-to-spine
git merge --ff-only loop/2026-07-22-claudette-prompt-to-spine   # disjorn-port is at 63d8ef1, the branch base
```

(`--ff-only` on `disjorn-port`: if it refuses, something else landed since
`63d8ef1` — stop and look.)

### 6. Guard the LEGACY Discord bot before its next restart

`claudette.service` (Discord `bot.py`, host-side, same repo) keeps running
old code until restarted — but after step 5 its NEXT restart imports the new
`core.py`, and `/opt/spine` doesn't exist on the host. Give the host runtime
its declared spine dir (one line in the host-local, gitignored config):

```sh
echo 'CLAUDETTE_SPINE_DIR = "/home/plink/bots/claudette/spine"' >> /home/plink/bots/claudette/config.py
```

(The canonical dir, not the mirror: this process runs as plink, and the
mirror is for res-* uids. Her container config.py copy needs NO line —
`/opt/spine` is the default.)

### 7. Propagate to her container and restart (her one lever pull)

```sh
cd /home/plink/bots/claudette && ./claudette-update.sh
```

(bundle → ff-only merge into her volume clone → restart `resident-cc`.)

### 8. Watch her come up, then verify live

```sh
sudo tail -f /home/res-claudette/logs/disjorn_bot.log
# expect: "Starting Disjorn adapter ... (prompt suffix ON)" then
#         "[Claudette] connected to Disjorn as bot <id>"
```

Then in the app: a normal reply (composed prompt live), and a persona
sanity check — she should sound exactly like herself, because the bytes are
exactly hers.

### Rollback (cheap, any time)

```sh
sudo systemctl --user -M res-claudette@ stop resident-cc
sudo -u res-claudette git -C /home/res-claudette/resident-home/bots/claudette reset --hard 63d8ef1
sudo systemctl --user -M res-claudette@ start resident-cc
```

The spine dir/mirror/mount are inert to the old code (nothing reads them);
they can stay.

## Follow-ups (deliberately NOT in this build)

- **Consolidation activation on her spine**: set `[spine] dir =
  "/srv/disjorn-spine/claudette"` in the PLACED config
  (`/srv/disjorn-resident-config/res-claudette/consolidation/claudette.toml`)
  and mirror the edit in the repo copy
  (`harness/consolidation/config/claudette.toml`, whose comment block
  explicitly anticipates this). Separate diff: it changes a consolidation
  surface, and the house loader verification above says it will parse.
  Mind `min_spine_age_days` — fresh entries won't be judged for 30 days,
  which is correct.
- **Her `#custodian`-witnessed confirm**: the spec records her preference
  that the confirm be witnessed in #custodian; still not satisfied (this
  build posts nothing, per discipline). Consider posting the confirm +
  this APPLY when presenting the diff.
- **Her review**: owner Claudette. She cannot read either repo from her
  container; present the adapter diff and this directory's entries to her
  as literal text (the entries ARE her prompt — she can verify those
  directly; the byte-identical test is her guarantee for the recomposition).
