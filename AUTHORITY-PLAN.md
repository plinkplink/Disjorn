# AUTHORITY-PLAN — give the broker its own identity, take root off plink's login

**Status: DRAFT, awaiting plink's sign-off. Nothing here is built.**
Drafted 2026-07-22 at the keyboard, from KB-D0 (RED-TEAM-BACKLOG.md).
plink's ruling: *"skip the five-minute bandaid, write up the migration WP"*,
plus *"consider a new user with passwordless sudo that you can have, as well."*

## The problem

`/etc/sudoers.d/plink-nopasswd` grants `plink ALL=(ALL) NOPASSWD:ALL`.
Verified with `sudo -l -U plink`. The broker runs as plink.

Every narrow sudoers rule in this repo is therefore **decorative**. The
broker's unit header states the design plainly —

> Everything the broker does is plink-level work… The single genuinely
> privileged verb, restart-disjorn, runs `sudo -n systemctl restart disjorn`,
> which succeeds only because /etc/sudoers.d/90-disjorn-broker grants plink
> exactly that one command passwordless… The sudoers line, not NNP, is the
> privilege boundary.

— and that sentence is false today. So is the equivalent claim for the new
`91-disjorn-build` helper boundary. Anything reaching code execution as the
broker's uid already has unrestricted root.

**This does not weaken the resident→host walls.** Those are uid-, nftables-,
and mount-based, and they are real: verified this session that `res-*` cannot
write `/srv/disjorn-ro`, cannot write the spine mirror, cannot traverse
`/home/plink`. What is missing is the *broker→root* wall, which is the one
every sudoers file in the repo is written against.

Origin (plink, 2026-07-22): a holdover from letting Claude Code use sudo for
builds. It was convenience, not design — which is exactly why replacing it
needs a plan rather than a deletion.

## Honest security accounting — read before agreeing

plink asked for a dedicated passwordless-sudo account for the agent. Worth
being blunt about what that does and does not buy, because it is easy to
mistake for containment:

- **A `NOPASSWD:ALL` account is root-equivalent, whoever holds it.** Moving the
  blanket grant from plink's login to an agent account does **not** reduce the
  total privilege on the box.
- **What it does buy, and these are real:** (1) the *broker stops inheriting
  it* — that is the whole point, and it is what makes the narrow rules
  load-bearing; (2) revocable independently, without disturbing plink's own
  login; (3) a separate audit identity, so "what did the agent do as root" is
  answerable; (4) blast radius on credential loss is scoped to one account.
- **The residual risk, stated plainly:** anything that compromises a Claude
  Code session running as that account gets root. That includes prompt
  injection reaching a Bash tool call. Today that risk exists *and* is
  entangled with plink's personal login; after this it is isolated but not
  removed. If that trade is not wanted, the alternative is an agent account
  with a *narrow* grant list and a password prompt for anything else — slower,
  strictly safer, and a fair thing to prefer.

**Recommendation:** do WP-A1 (broker uid) regardless — it is the load-bearing
one and it stands on its own. Treat WP-A2 (agent account) as a separate
decision, and prefer scoping its grants over `ALL` if the friction is bearable.

## WP-A1 — the broker gets its own uid  *(the load-bearing one)*

Create a system user `disjorn-broker` (no login shell, no home, not in
`plink`'s groups) and run the daemon as it.

**Why this and not just revoking NOPASSWD:** even with a password on plink's
sudo, a broker running *as plink* can read everything in `/home/plink` —
including all three bots' hardcoded API keys and the production Disjorn DB. The
uid split is what contains that; the sudoers change alone is not.

**The real fallout, and it is the whole cost of this WP.** Two verbs currently
reach *into* `/home/plink`, which is 0700 and must stay that way:

| verb | reaches | fix |
|---|---|---|
| `run-server-tests` | `/home/plink/Disjorn/Disjorn/server` (pytest in the live working tree) | point at `/srv/disjorn-ro` |
| `classify-diff` | `/home/plink/…/harness/classifier/classify_diff.py` | relocate to `/usr/local/lib/disjorn/` |

**Both fixes are improvements, not workarounds** — worth saying because it
changes how much this WP costs:

- `run-server-tests` currently lets a resident trigger a pytest run against
  plink's **dirty working tree**, whose result depends on uncommitted local
  state and which can execute uncommitted test code. Against the RO mirror it
  tests a known commit. That is more correct, and it removes a genuine
  resident-influenced-execution surface nobody had flagged.
- `classify_diff.py` moving to `/usr/local/lib/disjorn/` is the pattern already
  used for `run-resident.sh`, `house_memory`, and the residency venv. It also
  takes the classifier — an authorization surface — out of a tree the agent
  edits daily.

**Also to re-own** (currently `root:plink` or plink-owned): `broker.toml`,
`verbs.toml`, `protected-paths.toml`, `broker-api-key`, `/var/log/disjorn-broker`,
`/var/lib/disjorn-broker/metrics.json`, the BL-D2 `build-logs` spool and its
sidecars, and `/run/disjorn-broker`. Prefer group `disjorn-broker` with plink in
it, so plink keeps read access to the audit trail without owning the files.

**Sudoers to repoint:** `90-disjorn-broker` and `91-disjorn-build` both name
`plink` as the caller and must name the new uid instead. The build helper also
resolves `--uid=res-<name>`; confirm a non-plink caller can still invoke it.

**Metrics timers** (`disjorn-metrics-build/daily`) run `User=plink` and write
`metrics.json`, which `read-metrics` serves. Either move them to the new uid or
make the state dir group-writable — decide deliberately, since the daily job
also posts to #custodian with the broker's identity.

**Verification (this is the point of the WP):** after cutover, prove the narrow
rules are load-bearing by demonstrating a *failure* — as the broker uid, try a
sudo command outside its grant list and confirm refusal. A green test suite
does not demonstrate this; only the refusal does.

## WP-A2 — a dedicated agent account  *(separate decision, see the accounting)*

A `ccagent`-style system user for Claude Code sessions, so revoking or
narrowing agent privilege never touches plink's login.

- Home outside `/home/plink`; **must not** be able to read `/home/plink`, or
  the isolation is cosmetic.
- Needs write access to `/home/plink/Disjorn/Disjorn` to be useful — so either
  the repo moves to a shared group location, or the account joins a `disjorn-dev`
  group that owns the worktree. **This is the fiddly part**: the agent needs the
  repo, the repo currently lives inside the 0700 home the agent must not read.
- Decide the grant shape: `NOPASSWD:ALL` (convenient, root-equivalent) vs. an
  enumerated list covering what these sessions actually do — `systemctl`,
  `install` into `/usr/local/lib/disjorn`, `sudoedit` of `/etc/disjorn-broker/*`,
  `-u res-*` for verification probes. This session's transcript is a good
  sample of the real command distribution; mine it before choosing.

## WP-A3 — revoke `plink-nopasswd`

Do this **last**, after A1 proves the broker works under its own uid.

**What breaks: nothing automated.** Verified 2026-07-22 — every daemon path is
already covered by a narrow rule (`90-disjorn-broker`, `91-disjorn-build`,
`bot-restart`, `cadence`), and `brokerd.py` uses `sudo -n` against exactly
those. The `harness/keyboard/0*.sh` scripts use sudo but are run by hand, where
a password prompt is correct.

Keep `plink ALL=(ALL) ALL` — plink retains full sudo, with a password. Prefer
removing the file over editing it to nothing, so its absence is obvious.

**Rollback** is restoring one 29-byte file; keep a copy off-box first.

## Order, and why

1. **A1** — the load-bearing change; do the two relocations first, since they
   are safe on their own and shrink the cutover to an ownership change.
2. **A2** — only if the accounting above is accepted; independent of A1.
3. **A3** — last, so a broken A1 never leaves plink unable to fix it.
4. **A4** — a verification pass that tries, and fails, to escape each boundary.
   Fold into the existing red-team rather than running it separately.

Do not start A3 before A1 is proven. The failure mode of the wrong order is
being locked out of a box whose repair tool is the thing you just revoked —
the same footgun Claudette named about the symmetric gate (BUILD-LOOP D-1).

## Open questions for plink

1. WP-A2's grant shape: `NOPASSWD:ALL` or an enumerated list? (I lean
   enumerated, and would rather hit a prompt than hold standing root.)
2. Does the agent account get repo write via a shared group, or does the
   worktree move out of `/home/plink` entirely?
3. Metrics timers: move to the broker uid, or leave as plink with group write?
4. Is `run-server-tests` against the RO mirror the behaviour you want
   permanently? It is a small semantic change residents will notice — tests
   run against last-merged, not against your bench.
