# Spec: A spec's Status line moves with its build (`building` / `built@` / `failed`)

> **RETROACTIVE (keyboard-built).** Built at the keyboard 2026-08-17 as the
> follow-on plink asked for to `board --mark-merged` (`fbd8cb5`); merged
> `b18cdb5`. Harness lane, so it belongs in Gable's review queue; recorded here
> so the symmetric-review rule is honoured in both directions. Review is
> post-merge.

## Request
- **Verbatim**: "`board --mark-merged` needs another small piece added: We
  need to also mark it 'in-progress' during the build, so that a bot doesn't
  get confused and start it again. So probably trigger the file append on
  `start_build`."
- **Requester**: plink
- **Origin**: keyboard session, 2026-08-17

## Agreed UX
SPECS/README.md already ruled "state lives in the file — Status moves draft →
confirmed → building → built@<branch> → merged (or failed)". Nothing wrote the
middle words. Now the broker writes them: `confirmed → building` at
`start-build` (before the started line); with the terminal banner, from the
same ladder the banner is derived from: published → `built@<branch>`, failed
any way → `failed`, only NO-COMMITS → `confirmed` again; a launch that never
ran → `confirmed`. `board --mark-merged` writes `merged` from any of those.
The gate refuses every word but `confirmed`, so a claimed spec cannot be
started twice; after `failed` a human sets it back to `confirmed`. Every
banner ends with `spec status: <word> (commit <sha>)` or `NOT updated — why`.

## Architecture notes
- `brokerd._stamp_spec_status`: a plumbing commit on the canonical repo's
  `main` (`[start_build].spec_repo`) — blob, throwaway index, commit-tree,
  `update-ref` with the old sha as CAS — never touching the keyboard's
  index/worktree except a courtesy `checkout HEAD -- <spec>` when HEAD is
  main and the file is provably clean; then the SAME fetch + `--ff-only`
  refresh `refresh-mirror` runs. Not an edit of the mirror file: a dirty
  mirror refuses the next refresh that touches that spec. Moves only FROM the
  expected word, so a keyboard-advanced spec is never overwritten. The one
  resident-influenced string (a failure reason) is flattened and its `--`
  runs broken before entering the HTML comment.
- `board --mark-merged` advances confirmed/building/built@*/failed via the
  broker's `replace_spec_status`; new board rows for built@ / failed /
  stuck-building. PROTOCOL.md, SPECS/README.md, broker.toml documented.
- Deploy: `spec_repo` added to `/etc/disjorn-broker/broker.toml`; broker
  restarted; `/srv/disjorn-ro` chowned plink (root-run fetches had left 243
  root-owned object paths a plink-run refresh could not write) — refresh the
  mirror as plink, no sudo, from now on.
- Tests: `harness/broker/tests/test_broker_spec_status.py` (25, against a
  real git repo in the fixture); broker suite 266. Proven live the same day
  on `2026-08-08-per-channel-membership`: building → failed → confirmed
  (plink) → building → built@ → merged, all in the file.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: builder/harness (broker, keyboard tools). Review owner: **Gable**.

## Builder
- **Builder**: keyboard (plink's Claude Code seat, Fable).

## Expected diff tier
Tier 1 (harness bookkeeping; the confirm gate itself is unchanged and the
stamp cannot produce `confirmed` from anything but `building`).

## Token estimate
Spent: one keyboard session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: <retro-confirm — same one-liner; keyboard fills>
- **Confirmed at**: 2026-08-18

## Status
merged
<!-- keyboard-built; merged as b18cdb5 on 2026-08-17. -->
