# BUILD-SEAT-CONTRACT — what a build seat is given, and what it can do with it

**Status:** built 2026-08-12 from `SPECS/2026-08-08-gable-build-lane-provisioning.md`
(confirmed by plink, #custodian seq 1008). Nothing here is live until a human
merges the branch and runs the keyboard steps in § Provisioning a lane.

One artifact, five surfaces: **mounts, kernel, credentials, verbs, exit.** It
exists because the build seat's ground was previously described across
`run-build.sh`'s comments, `disjorn-build-launch`'s constants, `broker.toml`,
two specs and a runbook — six places, and the answer to "does the build seat
have a spine" was different in two of them on the same day. A seat whose
provisioning has no single description is a seat that gets provisioned twice,
differently.

Scope: the **build seat**, meaning a detached `start-build` session — the
container `run-build.sh` starts, under the transient unit
`disjorn-build-<slug>.service`, running as `res-<name>`. Not the resident seat
(`run-resident.sh`, a summon); not the keyboard seat.

The governing distinction, unchanged since 2026-08-06 ("branch B"): **the build
seat is a tool the resident uses, not the resident.** It has its own home, its
own config, its own kernel and its own author identity in git
(`disjorn-build <build@disjorn.local>`), so an audit trail never reads as though
Claudette or Gable typed it by hand.

---

## 1. Mounts

Everything the seat can see or touch, and nothing else. Set by
`disjorn-build-launch` (host-side env) and applied by `run-build.sh`.

| Container path | Source | Mode | Why |
|---|---|---|---|
| `/home/resident` | `/home/res-<name>/build-home` | **rw** | The seat's own home. Holds `~/work` (the clones, branch already checked out) and `~/.claude/CLAUDE.md`. **Not** the resident's home volume — sharing that is what booted a build into the resident's house and made it read her "do not act" placeholder. |
| `/config` | `/srv/disjorn-build-config/<name>` | ro | The seat's own `settings.json` and `env`. Separate from the resident's config root, same reasoning. |
| `/config/env` | `/dev/null` | ro | The credential file, masked from inside. Hygiene, not a wall — see § 3. |
| `/opt/disjorn` | `/srv/disjorn-ro` | ro | The committed repo mirror. The container-side prefix `[residents.<r>.path_map]` maps, so a build can `classify-diff` its own work. |
| `/opt/spine` | `/srv/disjorn-spine/<name>` | ro | The spine mirror, **when one is published** — see § 2. |
| `/run/gatehouse` | `/var/lib/disjorn-broker/gatehouse` | **rw** | The one writable path out. Bare repos; see § 5. |

**Absent on purpose, and each absence is a tested property:**

- **No broker socket.** See § 4.
- **No `/opt/house_memory`.** The deployed copy is a read-only non-repo, and
  mounting it is what led a blocked session to conclude that half of its spec
  "had nowhere to land". The build seat edits `harness/house_memory/` where it
  lives, in its clone; installing it is a keyboard step after merge.
- **No resident home, no `/home/plink`, no live working tree.** `/home/plink` is
  0700 and rootless podman cannot mount it; the working tree carries `data/`
  including the production DB, which is a privacy wall rather than an
  inconvenience.

Egress: `pasta`, with the real wall being host nftables on the `res-*` uid
(WP-H2). External git does not work from inside a build, by design.

---

## 2. Kernel

**What the session is told about itself.**

Today: `build-kernel.md`, copied by `run-build.sh` into
`~/.claude/CLAUDE.md` before launch. About forty lines: the task, the ground,
the five rules, the JSON report shape. No house rules, no biography.

**The spine mount is provisioned as of 2026-08-12; the cutover is not.** These
are two different things and conflating them is how a live kernel gets
redirected by accident.

- `disjorn-build-launch` sets `RESIDENT_SPINE_HOST=/srv/disjorn-spine/<name>`
  **if that directory exists**, and says so on stderr if it does not. Only a
  resident with a published mirror gets one; a resident without one launches
  byte-for-byte the invocation they launch today. (Gable has a mirror. Claudette
  does not, and this spec leaves her lane untouched.)
- `run-build.sh` mounts it read-only at `/opt/spine` and **refuses to launch**
  if the source is writable by the uid it runs as. Three independent walls:
  host ownership (plink:plink), the `:ro` bind, and that refusal.
- **Nothing reads it yet.** `[start_build].session_argv` execs `claude`
  directly, with no `bootstrap.py` call in front of it (compare
  `residency/summon.toml.template`, which has one). So `/opt/spine` is present
  and unread.

**The cutover, when plink wants it, is two lines that move together:**

1. `RESIDENT_SPINE_DIR=/opt/spine` in `/srv/disjorn-build-config/<name>/env`;
2. the bootstrap call in `[start_build].session_argv`, matching the summon's
   shape: `python3 /opt/house_memory/house_memory/bootstrap.py >&2 && exec claude …`.

**Take both or neither.** Line 1 without line 2 does nothing. Line 2 without
line 1 makes `bootstrap.py` read whatever `RESIDENT_SPINE_DIR` defaults to.
And note what line 2 does that is easy to miss: **`bootstrap.py` writes
`~/.claude/CLAUDE.md`, so it overwrites the copied `build-kernel.md`.** One of
the two is the kernel; never both. If the spine becomes the kernel, the task
framing that currently lives in `build-kernel.md` has to arrive some other way
(the spec on stdin already carries the task; the five rules do not).

**What the build seat loads if it does load a spine.** `RESIDENT_SEAT=build` is
passed by the wrapper and read by `bootstrap.py`: the **operational set only**
— `00-nonnegotiables`, `10-people`, `20-load-bearing-walls`, `30-build-rhythm`,
`40-cautions` — and never biography (`05-bearings`, `50-genesis`). *House
knowledge travels, biography doesn't.* Every entry is **baked**, not retrieved:
a detached build has no retrieval loop, so an un-baked operational entry is
simply absent, and "the build seat does load-bearing work with walls it's never
read" is the failure that arrangement prevents. Seat membership is **declared
in each entry's frontmatter, never inferred**, and the assembler fails loud on
a missing declaration or a missing `00` (Gable's binding redlines, 2026-07-23).

### Why this reverses a decision made six days earlier

On 2026-08-06 the spine was removed from the build seat entirely. Two grounds
were given. The first — a build session is a tool that lives for one spec, not
a resident that persists — **still stands, and is why this file exists.** The
second was that it *could not have worked*: `assemble_for_seat("build")` raises
"no kernel entry visible to seat 'build'".

That second ground was measured against **Claudette's** spine, every entry of
which declares `seats: [resident]`, and generalised into a claim about the
seat. **Gable's spine has declared a build seat since
`SPECS/2026-07-22-gable-spine-ro-cutover-seat-split.md`**, applied and verified
live on 07-23: resident 7 entries, build 5, stamped `(seat: build)`. The
arrangement was reviewed and blessed by its own review owner, who called baking
"correct, not a compromise".

Recorded at length because the shape recurs and this house has a name for it:
a claim measured in one place, written down as general, and then obeyed. The
07-22 spec's own standing lesson applies to its reversal too — *bake-affecting
acceptance is artifact-vs-yesterday, never mechanism-vs-itself*. If the cutover
happens, diff the assembled build kernel against the previous one and read it,
rather than checking that the assembler agrees with the assembler.

---

## 3. Credentials

**Routing is by seat, and the build seat is Max-only.**

Per `SPECS/2026-08-05-credential-routing-and-halt-protocol.md` §1: conversational
seats run on metered per-seat API keys; build and agent loops run on plink's Max
account. The seat split *is* the routing table, and it is also the failover
isolation — a Max limit halts the loop and leaves the chat seats untouched,
because they never shared a credential.

Mechanically, in the wrappers:

- The credential comes from `$RESIDENT_CONFIG_DIR/env` and nowhere else. The
  wrapper ignores its own environment, so a key in a systemd unit cannot become
  a session's identity.
- It is passed to podman by **name only** (`--env VAR`), never `--env VAR=value`
  — a value in argv is a value in `/proc/*/cmdline` for every uid on the box.
- Exactly one credential reaches the container. If both are in the file, the
  OAuth token wins and the API key is filtered out of the env-file copy podman
  reads.
- **The build seat refuses to launch on an API key alone.** Each wrapper sets
  `_seat_metered_fallback` immediately above the shared credential block —
  `allow` for `run-resident.sh`, `refuse` for `run-build.sh`. That one line is
  the entire seat difference, and it lives outside the byte-identical block so
  the block cannot drift.

**Why refuse rather than warn and continue.** The halt protocol says it in as
many words: *no polling, no retry loop, no silent resume, no silent
key-fallback.* A loop that quietly fails over from Max to the metered key has
converted "your account said not now" into "spend your API money instead",
which is exactly the decision plink reserved for himself (#custodian seqs
683/697). A build is also the most expensive thing in the house to run
accidentally on metered credit: it is a long session by construction.

**This is not the full fix and must not be recorded as one.** The spec's actual
design is a broker-side injecting proxy: the container holds a worthless dummy,
the proxy holds both real credentials and injects exactly one, and the route
flip is plink's hand on the host — so silent fallback becomes impossible *by
construction* rather than by refusal. That proxy is not built. Until it is, a
real credential is in the build container's environment and the session can read
it out of `/proc/self/environ`; the `/dev/null` mask over `/config/env` removes
the file copy only and cannot remove that. What this contract adds is the one
line the proxy would also enforce, at the one place a build can spend.

**Related and open:** KB-D6 is re-scoped by that spec from "the dominant exfil
path" to the probe that verifies the proxy wall once it exists.

---

## 4. Verbs

**The build container's verb surface is propose/read only, and it is realised
as *no broker socket at all*.**

A build has no `/run/disjorn-broker` mount, so it cannot call any verb. That is
a strict subset of propose/read, and it is deliberately enforced at the **mount**
rather than at a filter:

- **No `start-build` from inside a build.** A build seat that could start a build
  is nine slots deep by lunchtime. It happened: a 2026-08-05 session could see
  `broker start-build` from inside a build because the socket dir was mounted.
- **No restart verbs.** There is no `restart-self` verb at all (plink's ruling
  #3, AGENTHOOD.md), and `restart-disjorn` restarts the *platform*.
- Removing the mount kills the whole class rather than filtering a list, which
  is this house's preferred shape: walls are physical.

A build does not need verbs. Its worktree is writable, its remote is the
gatehouse, and its report is its stdout — which the broker's reaper already
reads and posts.

**BR-1 is the precondition for any widening, and BR-1 is open.** `verbs.toml`
switches are per **resident uid**, and the socket presents that uid in
`SO_PEERCRED` — so the broker cannot tell a build seat from the resident seat.
Granting the build container `file-proposal` would grant it to something wearing
the resident's identity, with no way for the audit to say which acted.

The same defect bites this lane from the other side. `[start_build].resident` is
a **global config value**, not derived from the caller, so whoever invokes
`start-build`, the build runs — and audits — as that one configured identity.
DEFERRED.md is explicit: *"Required before a second resident gets the verb,
because with one resident the identity happens to be right whenever she is the
caller, and with two it is wrong half the time."* **Gable's lane is that second
resident.** Nothing in this contract fixes it; the fix is deriving the build
identity from the caller's `SO_PEERCRED` uid in `brokerd.py`.
`09-build-lane-preflight.sh` §5 reports the condition whenever two residents
hold the verb, so the ruling gets made with the fact in front of it.

---

## 5. Exit

**One way out: a `loop/<slug>` branch in a bare gatehouse repo. A human reads
the diff and merges.**

- The branch name is `loop/<slug>`, and the slug keeps its `YYYY-MM-DD-` prefix,
  so **branch name == spec basename, 1:1** (BL-D4). Any `loop/…` branch traces
  back to exactly one spec with no lookup.
- The gatehouse holds **bare** repos: no working tree, so a push deploys nothing
  and merges nothing. That is the wall, not the group bits.
- `run-build.sh` clones **every** `*.git` in the gatehouse, fresh, per run, and
  rewrites each `origin` to the container-side path. Fresh every time on
  purpose: a stale checkout that silently builds against yesterday's tree is a
  failure class this house has already spent days on.
- The build pushes with `git push origin HEAD` and stops. Nothing lands itself.

### The repo recipe, applied at creation

A gatehouse repo is written by two uids — the broker user reads and merges out
of it, `res-<name>` pushes into it — so it needs, **together and from the first
byte**: owner = the broker user, group = `gatehouse`, **setgid on every
directory**, `g+rwX`, `core.sharedRepository=group`, and a per-repo
`safe.directory` entry in `/etc/gitconfig` (git refuses a repo owned by another
uid since CVE-2022-24765, which this layout trips by design).

`harness/keyboard/08-gatehouse-repo.sh create <repo> <resident…>` is that
recipe, and it always finishes by verifying itself.

**Applied at creation, not repaired after**, because setgid is the property that
governs files *that do not exist yet*. Retrofitting fixes today's objects and
not tomorrow's, and the symptom arrives weeks later as one object file in one
fan-out directory carrying the wrong group, surfacing as "Permission denied" on
a fetch that has worked a hundred times.

**The group layer is verified from a resident seat, never keyboard-reported**
(Build-A lesson, 2026-08-07). Every mode-bit check answers "do these look right
to root", and root is the one uid for which that answer is always yes. So
`verify` has `res-<name>` write a real (dangling, then deleted) object with
`git hash-object -w`, and checks the group and mode of what landed.

### Push `main` back to the gatehouse after every merge

**The stale-base hazard, which fired live on 2026-08-07.** A build clones from
the gatehouse, so gatehouse `main` is the base *every* build starts from. Merge
a branch into the canonical repo and forget to push `main` back, and the next
build branches off yesterday's tree — a conflict at merge time if you are lucky,
and a quiet revert of the previous merge if you are not.

So the merge cycle is **fetch → review → merge → push `main` back**, and the
last step is not optional. `09-build-lane-preflight.sh` §3 compares gatehouse
`main` against canonical `main` and fails when they diverge; it is how you find
out you forgot.

### Two things the exit does NOT have

- **No branch-namespace enforcement.** `MERGE-CONTRACT.md` specifies a
  pre-receive hook keeping residents out of `main` and out of each other's
  namespaces. It is **not built.** Until it is, "a resident cannot move gatehouse
  main" is a fact about nobody having tried, not a wall. This is not new — the
  gatehouse has been mounted rw into Claudette's container since 2026-08-05 —
  but it is worth naming in the file that describes the exit.
- **No merge verb for Tier 2, ever.** The human gate is out-of-band by design.

---

## One thing this build did NOT decide: the daily cap

`SPECS/2026-08-08-gable-build-lane-provisioning.md` § Agreed UX describes the
lane as *"exactly as Claudette does today: spec-gated, **two slots/day**"*. The
configured cap is **10**, ruled by plink on 2026-08-05 (BR-3, DEFERRED.md),
which closed a defect whose whole shape was a proposed number hardening into a
configured number by time passing — `daily_build_cap = 2` was a *proposal*
BUILD-LOOP.md flagged as "plink tunes at staging time", and two documents then
quoted it as "the ratified default" without anyone ratifying it.

So the spec's clause is a **description of the existing UX that does not match
the ruled value**, not an architecture note asking for a change, and this build
left the cap alone rather than reintroducing the exact number the 08-05 ruling
retired. If two is wanted for this lane specifically, the mechanism is already
there and it is one deliberate edit:

```toml
[start_build.per_resident.res-gable]
daily_build_cap = 2
```

**plink's call, not a build's.** Flagged here so the discrepancy is written down
somewhere rather than resolved by whoever notices it next.

---

## Provisioning a lane

Repo-side (merged with this branch) and host-side (plink's hands) are split as
the spec splits them. **Do the host steps in this order.**

1. **Merge this branch**, then deploy the code artifacts it changes:
   ```
   sudo install -m 0755 harness/cc/run-build.sh          /usr/local/lib/disjorn/run-build.sh
   sudo install -m 0755 harness/cc/run-resident.sh       /usr/local/lib/disjorn/run-resident.sh
   sudo install -m 0755 harness/broker/disjorn-build-launch /usr/local/lib/disjorn/disjorn-build-launch
   sudo install -m 0755 harness/cc/build-kernel.md       /usr/local/lib/disjorn/build-kernel.md
   ```
   `run-resident.sh` is a **live change to the summon path** (the shared
   credential block moved). Watch one summon after.
2. **Push `main` back to the gatehouse** — the merge you just did is exactly the
   event § 5 is about.
3. **Create the lane's gatehouse repo** with the recipe:
   ```
   sudo bash harness/keyboard/08-gatehouse-repo.sh create gable gable
   ```
4. **Publish the spine mirror** (only if this seat should have one):
   ```
   sudo bash harness/keyboard/06-spine-mirror.sh gable
   ```
   This is the mount, not the cutover. § 2 has the cutover.
5. **Build config**: `/srv/disjorn-build-config/<name>/` needs `settings.json`
   (template: `harness/cc/build-config/settings.json`) and `env` with a
   `CLAUDE_CODE_OAUTH_TOKEN`, mode 0600. An API key alone will refuse to launch.
6. **Build home**: `/home/res-<name>/build-home/` must exist, owned by
   `res-<name>`.
7. **Sudoers**: `/etc/sudoers.d/91-disjorn-build` must name this resident. One
   resident per line, on purpose — adding one is a keyboard act with a visible
   diff. `visudo -cf` the repo copy before installing it.
8. **Pre-flight, and read every line of it:**
   ```
   sudo bash harness/keyboard/09-build-lane-preflight.sh gable
   ```
9. **Only then** flip `"start-build" = true` for this resident in
   `/etc/disjorn-broker/verbs.toml` (sudoedit). Read § 4 on BR-1 first: a second
   resident holding this verb is the condition DEFERRED.md says to close before
   granting it.

---

## Pre-flight

`sudo bash harness/keyboard/09-build-lane-preflight.sh <resident>` — read-only,
run before the first build in a lane and again after anything is deployed.

1. **Deployed vs repo**, for every code artifact. The stale-deploy family is
   this project's most reliable way to lose an evening, and it has fired at
   least four times. Config (`broker.toml`, `verbs.toml`) is state and is
   *meant* to differ — that diff is printed for reading and never counted as a
   failure, because a report that cries wolf about expected differences is a
   report nobody reads.
2. **The gatehouse repo**: the full recipe, the wrong-group file check, and the
   resident-seat probe (delegated to `08-gatehouse-repo.sh verify`).
3. **Gatehouse `main` vs canonical `main`** — the stale-base check.
4. **The seat's launch-blocking ground**: spine mirror present and unwritable
   *from the seat*; build config dir, `settings.json`, credential route (names
   only, never values) and file mode; build home.
5. **The verb surface**: which residents hold `start-build`, what
   `[start_build].resident` says, the BR-1 condition when both are true, and
   that the deployed `run-build.sh` still mounts no broker socket.

---

## Where the pieces live

| Thing | File |
|---|---|
| The container, mounts, credential decision, spine refusal | `harness/cc/run-build.sh` |
| Host-side env, uid, unit, kernel-enforced limits | `harness/broker/disjorn-build-launch` |
| What the session is told about itself (today) | `harness/cc/build-kernel.md` |
| The seat's permission set | `harness/cc/build-config/settings.json` |
| The verb: gate, budget, narration, reattachment | `harness/broker/PROTOCOL.md` § `start-build` |
| Verb config, model pin, specs dir, caps | `harness/broker/broker.toml` § `[start_build]` |
| The kill switches | `/etc/disjorn-broker/verbs.toml` (template: `harness/broker/verbs.toml`) |
| The sudo boundary | `harness/keyboard/91-disjorn-build.sudoers` |
| Gatehouse repo creation + verification | `harness/keyboard/08-gatehouse-repo.sh` |
| Lane pre-flight | `harness/keyboard/09-build-lane-preflight.sh` |
| Spine mirror publication | `harness/keyboard/06-spine-mirror.sh` |
| The merge flow this feeds | `harness/cc/MERGE-CONTRACT.md` |
| Seat split, baking, declared seats | `SPECS/2026-07-22-gable-spine-ro-cutover-seat-split.md` |
| Credential routing + halt protocol | `SPECS/2026-08-05-credential-routing-and-halt-protocol.md` |
| This lane's spec | `SPECS/2026-08-08-gable-build-lane-provisioning.md` |
