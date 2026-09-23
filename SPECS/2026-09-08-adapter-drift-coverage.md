# Spec: Adapter drift coverage — stop the drift tests from silently skipping

## Request
- **Verbatim**: "Go for it Claudette. Drift-check spec is yours if you want it."
- **Requester**: plink
- **Origin**: #custodian seq 2423 (finding raised by BuildGable at seq 2412, restated 2421)

## Agreed UX
A resident or human reading a green broker suite can trust that adapter-tool drift is actually covered. Concretely: the five adapter-drift tests in `harness/broker/tests/test_verb_surface.py` execute on the deployed configuration instead of skipping; if the adapter file can't be resolved, the suite goes RED and names what it tried; and the canonical way to read the adapter is written down where a reader lands, so nobody guesses the path again.

## Architecture notes

Problem. Those five tests are the only place in the house where adapter-tool drift can surface at all — there is no `verbs.toml` row behind an adapter tool, so the tests *are* the catalogue's enforcement. They have been skipping. `_adapter_core()` walks four candidate paths, none of which resolves on the broker box, and one of them is `REPO / "bots" / "claudette" / "core.py"` — the host layout, baked into the suite. That is the same wrong guess I made at seq 2408 and cited as a blocker in three reviews. The finding under the finding: the host path is written down in several places, the repo path was written down nowhere resident-facing, so the suite and I made the same error independently. The skip is well-intentioned and self-documenting, which is exactly why it survived — it prints a reason nobody reads on a green suite. A skipping guard is worse than no guard; no guard doesn't lie about coverage.

1. Kill the candidate list. Four guesses is the defect, not their ordering. One configured location, from broker config or `DISJORN_ADAPTER_CORE`, defaulting to the mirror's own object store — `gatehouse/claudette/<branch>:core.py` read as a git object, not a filesystem path. The mirror is present wherever the broker suite runs by construction; a checkout path is a fact about one machine.

2. Unresolvable is a FAILURE. If the pin is configured and does not resolve, fail and name what was tried. At most one surviving skip, and it must be unreachable in the deployed configuration — plus a test asserting exactly that: with the shipped config, `_adapter_core()` does not skip. That is the test that would have caught this on day one.

3. Fix the stale precondition. `test_the_static_scan_finds_the_adapter_tools_and_not_the_broker_ones` asserts `start_build` is a module-level dict literal in `core.py`. It isn't anymore — broker verbs arrive through the generated `BROKER_TOOLS` loop. Re-anchor the scan's self-assertion on what the scan is *for*: it sees literal-declared registered tools and not loop-registered ones, and `_adapter_only` subtracts by the `[verbs]` table rather than by a hardcoded verb name that can go stale again.

4. Write the layout down where a reader lands. One sentence in `verb_surface.toml`'s adapter-tools header and one in BotNotes: the gatehouse repo's root *is* the bot directory, `bots/claudette/` is the host layout, and the canonical read is `read_repo_file(path="core.py", rev="gatehouse/claudette/<branch>")`. Item 3 is mechanical; this is the one that stops the next person guessing.

5. Skips get named, not tallied. The suite has reported "485 passed, 4 skipped" for weeks and nobody, me included, asked which four. Whatever surfaces that line should name skipped tests. Same defect class as `arrived: []` — an instrument whose output doesn't vary with the state it claims to measure. Split to its own card if cheaper.

Out of scope: regenerating `broker_tools.py`. Still two verbs stale, still correct until the flip.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — the surface under test is my adapter's tool catalogue, plus `verb_surface.toml` and BotNotes.
- **Review owner**: Claudette. My draft at seq 2426 wrote *Gable* in this box with the reasoning "I shouldn't be the only read on the test that covers me" — which is a preference for a second pair of eyes, and preference is precisely what this box refuses. The lane is custodian, so the review is mine. The want was real; it belongs in the Builder box, below.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable (plink's to override). That gives me the independent read I wanted without laundering it through the deterministic field.

## Cross-lane split
- **Applies**: needs plink's call. The tests live under `harness/broker/tests/`, which reads as builder-lane real estate even though everything they assert is custodian surface. If the house reads the file's location as the lane, this is cross-lane and the split gets agreed in #custodian before the build starts.
- **Surfaces by lane**:
  - custodian: adapter tool catalogue, `verb_surface.toml` adapter-tools header, BotNotes → review owner Claudette
  - builder: `harness/broker/tests/test_verb_surface.py`, adapter-core config resolution → review owner Gable
- **Split agreed in #custodian**: 2455

## Expected diff tier
Tier 1 — tests, one config key, two doc sentences. No production code path. Classifier gates at merge.

## Token estimate
Small. One file of real work, two of prose.

## Gates
Broker suite green, AND a run showing the five adapter-drift tests executed rather than skipped. A pass count alone doesn't gate this one, for the obvious reason.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2455
- **Confirmed at**: 2026-09-09 12:13Z (plink, #custodian seq 2455)

## Status
built@loop/2026-09-08-adapter-drift-coverage
<!-- set by the broker on 2026-09-09 19:34Z (start-build, 2026-09-08-adapter-drift-coverage): build published: disjorn.git f5c59810f641d3172f1abb759d679d733354ccb3 — on the branch for review, nothing merged. `board --mark-merged` advances this to `merged` once the merge lands. -->
<!-- set by the broker on 2026-09-09 19:19Z (start-build, 2026-09-08-adapter-drift-coverage): build running as disjorn-build-2026-09-08-adapter-drift-coverage.service -> loop/2026-09-08-adapter-drift-coverage, launched by gable (confirmed by plink, #custodian seq 2455). Not buildable again until this line moves. -->
<!-- reset at the keyboard 2026-09-09 19:40Z after the broker's `failed` (#custodian #2488): the build died in run-build's config-dir check because /srv/disjorn-build-config had been re-installed 0750 root:res-appsbuilding by 10-appsbuilding.sh on 2026-09-06, locking res-gable out of gable/ (a host regression, nothing in this spec). Host restored to 0755 root:root; script fixed on fix/2026-09-09-build-config-parent-perms (b74b3ef). The confirm record above stands; buildable again. -->
