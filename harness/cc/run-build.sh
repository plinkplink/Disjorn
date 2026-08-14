#!/usr/bin/env bash
# run-build.sh — WP-L4: podman run wrapper for one DETACHED build session.
#
# The sibling of run-resident.sh. Same container/user/mount discipline, but for
# a build-from-a-confirmed-spec session rather than a summon: the worktree is
# read-write (the build writes code and commits to a branch), the wall-clock cap
# is longer (a build is a whole feature, not a chat turn — enforced by the
# broker reaper, documented below), and the spec arrives on STDIN.
#
# LAUNCHED BY the disjorn-broker `start-build` verb (harness/broker/brokerd.py),
# which execs this DETACHED (start_new_session) and does not wait; a reaper
# thread feeds the spec on stdin and enforces the cap. Like run-resident.sh this
# script is only the faithful forwarder — it decides no policy. The confirm
# gate, the budget, and the branch name all live in the broker; here we start
# the container and forward the pinned model.
#
# Usage:
#   run-build.sh <resident-name> <slug> [command...]
#     <resident-name>  e.g. "gable" (no res- prefix) — the identity the build
#                      runs as (keep-id; SO_PEERCRED at the broker socket).
#                      HOW that identity is actually acquired (WP-L4's open
#                      fork, closed 2026-07-22): the broker does NOT exec this
#                      script directly. It runs
#                        sudo -n /usr/local/lib/disjorn/disjorn-build-launch run <name> <slug> ...
#                      and that helper does `systemd-run --uid=res-<name>`, so
#                      the uid is set by PID 1 before exec — not by a userspace
#                      privilege drop inside a sudo'd process. That is what
#                      makes keep-id and SO_PEERCRED true rather than aspirational:
#                      run directly by plink, podman would map the container to
#                      uid 1000 and $HOME would resolve to the wrong tree entirely.
#     <slug>           the spec slug; branch is loop/<slug> and the slug KEEPS
#                      its YYYY-MM-DD- prefix (BL-D4), so branch name == spec
#                      basename 1:1. The container name and the transient unit
#                      name deliberately share the `disjorn-build-<slug>` stem.
#                      Broker-validated kebab, safe as an arg.
#     [command...]     the headless CC build-session argv, forwarded verbatim.
#                      Carries the WP-L5 model pin: the broker appends
#                      `--model <id>`, which rides into the container command via
#                      "$@" (identical mechanism to run-resident.sh).
#
# The SPEC (the chat-derived design) is fed to the session on STDIN, never in
# argv — the launcher.py doctrine: argv is config, chat is data.
#
# NO MERGE, NO PROD: the build lands on its branch and waits for a human. The
# egress wall (WP-H2 host nftables on the res-* uid) blocks external git. As of
# 2026-08-13 there is NO writable path out of the container at all: the
# gatehouse mount is gone and the product leaves by the POST-EXIT HARVEST at
# the bottom of this file, which runs here on the host. Human merges; nothing
# lands itself.
#
# WHY THE HARVEST EXISTS (the 08-13 finding, SPECS/2026-08-13-build-publish-path.md,
# confirmed #custodian seq 1211). `podman --userns keep-id` does NOT map
# supplementary groups, so the host's `gatehouse` group does not exist inside
# the container: an in-container `git push` into the mounted gatehouse worked
# only by uid-ownership accident, and the objects it wrote carried whatever
# group the accident produced. Out here the wrapper already runs as res-<name>,
# where the gatehouse group and the /etc/gitconfig `safe.directory` exemptions
# actually apply. So the push moved to the one place it was ever well-defined,
# and the container lost the mount that made the accident possible.
#
# BRANCH B (2026-08-06). This wrapper provisions a TOOL, not a resident. It has
# no broker socket, no house_memory mount, and its own home and config. See
# build-kernel.md for everything the session is told about itself. Both
# remaining deletions have a rationale block below; read those before adding
# either back.
#
# BRANCH B'S THIRD DELETION IS REVERSED (2026-08-12): the spine mount is back,
# for both seats, per SPECS/2026-08-08-gable-build-lane-provisioning.md
# (confirmed by plink, #custodian seq 1008). The reasoning it was removed on
# held for Claudette's spine and not for Gable's, which has declared a build
# seat since the 07-22 seat-split. Mounting is still not the cutover — see the
# spine mount block. The full contract for this seat, all five surfaces, is
# harness/cc/BUILD-SEAT-CONTRACT.md.
#
# Overridable env (defaults are the production layout — mirror run-resident.sh):
#   RESIDENT_IMAGE           image ref        (localhost/disjorn-resident:latest)
#   RESIDENT_HOME_VOL        host build home  ($HOME/build-home) — the BUILD
#                            seat's own home, NOT the resident's. Holds ~/work
#                            (the clones) and ~/.claude/CLAUDE.md (the task
#                            kernel). Mounted RW.
#   RESIDENT_CONFIG_DIR      host config dir  (/home/plink/build-config/<name>)
#                            — the BUILD seat's own settings.json, separate
#                            from the resident's.
#   RESIDENT_BUILD_KERNEL    task kernel file (/usr/local/lib/disjorn/build-kernel.md)
#                            copied to ~/.claude/CLAUDE.md before launch.
#   RESIDENT_GATEHOUSE       bare repo dir    (/var/lib/disjorn-broker/gatehouse)
#                            NOT mounted (2026-08-13). Read HERE, on the host:
#                            the provisioning loop clones out of it and the
#                            post-exit harvest publishes back into it. The
#                            container never sees it.
#   RESIDENT_NETWORK         podman network   (pasta; real egress wall is WP-H2)
#   RESIDENT_SPINE_HOST      host spine dir   (UNSET = no spine mount). Set by
#                            disjorn-build-launch to the plink-owned mirror
#                            /srv/disjorn-spine/<name>; mounted ro at
#                            /opt/spine. See the spine mount block below —
#                            mounting is NOT the cutover.
#   RESIDENT_PODMAN_EXTRA    extra podman-run flags (word-split; e.g. "-d")
#   RESIDENT_REAP            1 (default) = a watchdog sibling AND a signal trap
#                            kill this wrapper's container if the wrapper
#                            itself dies, so a refused/timed-out session cannot
#                            keep running — and, since 2026-08-13, cannot keep
#                            WRITING into ~/work while the next launch deletes
#                            it. 0 disables both (debugging only; warns
#                            loudly). Not armed for detached runs. See the
#                            container reaper block near the bottom.
#
# SECRETS: same mechanism as run-resident.sh — the session credential comes
# from $RESIDENT_CONFIG_DIR/env and nowhere else, never via argv. But NOT the
# same routing. The build seat is Max-only: CLAUDE_CODE_OAUTH_TOKEN (minted by
# `claude setup-token`) and nothing else. If the env file offers only
# ANTHROPIC_API_KEY this wrapper REFUSES TO LAUNCH rather than billing the
# metered key — the credential-routing spec's "no silent key-fallback",
# enforced at the one place a build can spend. See the credential block below,
# config-template/README.md, and BUILD-SEAT-CONTRACT.md § Credentials.
#
# WALL-CLOCK CAP: enforced by the broker reaper (start_build.timeout_sec,
# suggest 3600s), which kills the session and narrates a loud failure at the
# cap. This script does not embed the timeout, so the single source of truth
# stays the broker config.
#
# NOTE for the keyboard install: res-* users cannot read /home/plink — copy this
# script world-readable, e.g. /usr/local/lib/disjorn/run-build.sh, and point
# [start_build].command at it (KEYBOARD-NEXT.md 6b).
set -euo pipefail

NAME="${1:?usage: run-build.sh <resident-name> <slug> [command...]}"
shift
SLUG="${1:?usage: run-build.sh <resident-name> <slug> [command...]}"
shift

IMAGE="${RESIDENT_IMAGE:-localhost/disjorn-resident:latest}"
# Deterministic, and the single source of truth: --name below and the
# container reaper block at the bottom must always mean the same container.
CONTAINER_NAME="disjorn-build-$SLUG"
# SEAT SPLIT AT THE FILESYSTEM (2026-08-06, "branch B"). The build seat used
# to default to the RESIDENT's home volume and the RESIDENT's config dir —
# same `$HOME/resident-home`, same `/srv/disjorn-resident-config/<name>` — so a
# build session booted inside the resident's own house wearing the resident's
# own settings. That is what produced the 2026-08-05 blocked build: the session
# looked for the resident's assembled kernel, found the placeholder that says
# "do not act on substantive tasks", and correctly refused.
#
# The build seat is a TOOL, not the resident. It gets its own home, its own
# config, and its own kernel (build-kernel.md, ~40 lines, no house rules).
# "Is the builder Claudette or a thing Claudette uses" is settled here, in the
# mounts, not in a prompt: a tool that shares the resident's home is still
# wearing her clothes. The 08-12 spine restoration does not touch that — the
# spine arrives read-only, seat-filtered to the operational set, and it is the
# resident's SPINE, not the resident's HOME.
HOME_VOL="${RESIDENT_HOME_VOL:-$HOME/build-home}"
CONFIG_DIR="${RESIDENT_CONFIG_DIR:-/home/plink/build-config/$NAME}"
HOUSE_MEMORY="${RESIDENT_HOUSE_MEMORY:-/home/plink/Disjorn/Disjorn/harness/house_memory}"
NETWORK="${RESIDENT_NETWORK:-pasta}"
# The task kernel: what this session is told about who it is. Copied into the
# build home below, because Claude Code reads ~/.claude/CLAUDE.md and nothing
# in [start_build].session_argv assembles one — the build argv execs claude
# directly, with no bootstrap.py call in front of it (compare
# residency/summon.toml.template, which has one).
#
# STILL TRUE AFTER THE 08-12 SPINE RESTORATION, and this is the seam to watch:
# mounting /opt/spine does not make anything read it. The spine becomes the
# build seat's kernel only when plink sets RESIDENT_SPINE_DIR=/opt/spine in the
# build /config env file AND puts the bootstrap call in session_argv — at which
# point bootstrap.py OVERWRITES this copied file. One or the other is the
# kernel; never both. BUILD-SEAT-CONTRACT.md § Kernel carries the ordering.
BUILD_KERNEL="${RESIDENT_BUILD_KERNEL:-/usr/local/lib/disjorn/build-kernel.md}"
# The bare repos the build clones from and pushes to. Bare on purpose: no
# working tree means a push cannot deploy.
GATEHOUSE="${RESIDENT_GATEHOUSE:-/var/lib/disjorn-broker/gatehouse}"

[ -d "$HOME_VOL" ] || { echo "run-build: build home missing: $HOME_VOL" >&2; exit 1; }
[ -d "$CONFIG_DIR" ] || { echo "run-build: build config dir missing: $CONFIG_DIR" >&2; exit 1; }
[ -f "$BUILD_KERNEL" ] || { echo "run-build: build kernel missing: $BUILD_KERNEL" >&2; exit 1; }
[ -d "$GATEHOUSE" ] || { echo "run-build: gatehouse missing: $GATEHOUSE" >&2; exit 1; }

# ── BEGIN provisioning ───────────────────────────────────────────────────
# THE WRAPPER BUILDS THE GROUND; THE BUILDER STANDS ON IT.
#
# Everything below runs on the HOST as res-<name> (systemd-run set the uid), so
# it writes to the build home as its owner and needs no privilege at all. The
# session that starts afterwards finds clones already made and a branch already
# checked out, and spends none of its context on setup.
#
# This is deliberate division of labour, not convenience. Branch B says the
# builder does ONE thing; a builder that provisions itself has two jobs and a
# second way to fail. It also means a provisioning failure happens HERE — loud,
# before the model is ever invoked — instead of forty seconds into a session
# that then has to reason about whether its own ground is real.
BRANCH="loop/$SLUG"
WORK="$HOME_VOL/work"

# The task kernel. Copied, not mounted: Claude Code reads $HOME/.claude/CLAUDE.md
# and a bind mount there would fight the home volume.
mkdir -p "$HOME_VOL/.claude" "$WORK"
cp -f "$BUILD_KERNEL" "$HOME_VOL/.claude/CLAUDE.md"

# One FRESH clone per gatehouse repo, every run. Not an optimisation target: a
# local clone hardlinks its objects, so even the 80MB repo costs milliseconds
# and almost no disk. Re-cloning deletes a whole class of failure — the stale
# checkout that silently builds against yesterday's tree — and this week has
# already spent two days on exactly that shape (an uninstalled fix, a stale
# container serving old code). A build is a fresh attempt at a spec, never a
# continuation of a previous one's leftovers.
#
# THE ENTITLED SET, not "every *.git in the gatehouse" (Claudette's argument,
# 08-12/13; SPECS/2026-08-13-build-publish-path.md architecture note 1a). A
# build gets the disjorn tree and its OWN repo, and nothing else. The old glob
# handed every seat a writable clone of every other seat's repo — a widening
# that arrived by directory listing rather than by decision, and that grows
# silently every time the keyboard creates a lane. Foreign repos in the
# gatehouse are simply not cloned; a MISSING entitled repo is a loud failure
# before podman is ever invoked, because a build that quietly proceeds with
# half its ground is a build whose report cannot be trusted.
ENTITLED=( disjorn )
[ "$NAME" = "disjorn" ] || ENTITLED+=( "$NAME" )
# Clone-point sha per entitled repo, SAME INDEX as ENTITLED. Held in this
# process's memory and NOT in a file, deliberately: everything under $HOME_VOL
# is the container's rw home, so a base recorded there is a base the measured
# party can edit. Keeping it here is only possible because the launch tail no
# longer `exec`s — the wrapper outlives the container now, so it can remember
# what it cloned. This is the sha the harvest means by "beyond the clone point".
BASE_SHA=()

for _repo in "${ENTITLED[@]}"; do
  _repo_path="$GATEHOUSE/$_repo.git"
  _dest="$WORK/$_repo"
  [ -d "$_repo_path" ] || {
    echo "run-build: REFUSING TO LAUNCH: entitled repo missing from the gatehouse: $_repo_path" >&2
    echo "run-build: the entitled set for resident '$NAME' is: ${ENTITLED[*]}. Create it at the keyboard with 'harness/keyboard/08-gatehouse-repo.sh create $_repo $NAME' (the recipe applies ownership, setgid and safe.directory together, from the first byte)." >&2
    exit 1; }

  # ── QUARANTINE CLAUSE (1175, spec architecture note 1d) ────────────────
  # A FAILED HARVEST MAKES THE WORKSPACE CLONE UNDELETABLE. The `rm -rf`
  # below is correct for a clone whose work already reached the gatehouse and
  # catastrophic for one whose work did not: the 2026-08-13 rescue existed
  # only because a human posted a warning in-channel and nobody launched in
  # the meantime, and a human remembering is not a design.
  #
  # "Unharvested" is measured against the GATEHOUSE, not against a marker
  # file: for every loop/* branch in the existing clone, ask the gatehouse
  # whether some REF of its own still reaches that branch head. Reachability,
  # not object existence — the distinction is the 2026-08-08 incident
  # (Claudette, seq 1224): that push landed every OBJECT and then died at the
  # ref update, so a `cat-file -e` test reads the leftover as harvested and
  # the next launch deletes the only copy — the exact trap this clause
  # exists to close, wearing the fix as a costume. Object existence also
  # DECAYS: unreachable loose objects get gc-pruned after weeks, so the same
  # question would answer differently depending on when it is asked. A ref
  # contains it or it did not land. Checking the SHA against ALL refs (not
  # just `refs/heads/<branch>`) is what keeps a zero-commit leftover from
  # being quarantined forever — its head IS the clone point, so main
  # contains it, and a quarantine pile full of empty clones is a pile
  # nobody reads.
  #
  # Preserve and PROCEED (the spec offered "or refuse to launch"): the build
  # that is being launched now is a different spec's build and blocking it on
  # a previous spec's rescue punishes the wrong session. The QUARANTINED line
  # goes to STDOUT, not stderr, because stdout is what the broker reaper reads
  # and posts — the surfacing is the point.
  if [ -d "$_dest/.git" ]; then
    _q_branches=""
    while IFS= read -r _wbranch; do
      [ -n "$_wbranch" ] || continue
      _whead="$(git -C "$_dest" rev-parse --verify -q "refs/heads/$_wbranch" 2>/dev/null)" || continue
      [ -n "$_whead" ] || continue
      if git -C "$_repo_path" cat-file -e "$_whead^{commit}" 2>/dev/null \
         && [ -n "$(git -C "$_repo_path" for-each-ref --contains "$_whead" --count=1 2>/dev/null)" ]; then
        continue   # a gatehouse ref reaches it: harvested (or clone-point), safe to delete
      fi
      _q_branches="${_q_branches:+$_q_branches }$_wbranch"
    done < <(git -C "$_dest" for-each-ref --format='%(refname:short)' 'refs/heads/loop/*' 2>/dev/null)

    if [ -n "$_q_branches" ]; then
      _q_slug="${_q_branches%% *}"; _q_slug="${_q_slug#loop/}"
      mkdir -p "$HOME_VOL/quarantine"
      _q_path="$HOME_VOL/quarantine/$_repo-$_q_slug-$(date +%s)"
      mv "$_dest" "$_q_path" || {
        echo "run-build: REFUSING TO LAUNCH: could not quarantine $_dest -> $_q_path; it holds unharvested commits on: $_q_branches" >&2
        exit 1; }
      echo "QUARANTINED $_repo $_q_path"
      echo "run-build: $_dest held unharvested commits on: $_q_branches — moved to $_q_path rather than deleted. Nothing else will touch it; recover or discard it at the keyboard." >&2
    fi
    unset _q_branches _q_slug _q_path _wbranch _whead
  fi

  rm -rf "$_dest"
  git clone --quiet "$_repo_path" "$_dest" || {
    echo "run-build: FAILED to clone $_repo_path -> $_dest" >&2; exit 1; }
  git -C "$_dest" checkout --quiet -b "$BRANCH" || {
    echo "run-build: FAILED to create $BRANCH in $_dest" >&2; exit 1; }
  # Identity for the commits, local to the clone so the image needs no global
  # gitconfig. The author is the SEAT, not the resident: a build is a tool run,
  # and the audit should not read as though Claudette typed it.
  git -C "$_dest" config user.name  "disjorn-build"
  git -C "$_dest" config user.email "build@disjorn.local"
  # ORIGIN IS REMOVED, NOT REPOINTED (1175). Until 2026-08-13 this line
  # rewrote origin to /run/gatehouse, the container's view of the mount. The
  # mount is gone, so any origin at all is a trap: an in-container `git push
  # origin` against a path that looks real fails somewhere down in git's
  # transport with a message about a missing directory, and the session then
  # reasons about whether the gatehouse is broken. With no remote it fails at
  # the first hop, in one line, saying exactly the true thing — "'origin' does
  # not appear to be a git repository" — and build-kernel.md no longer asks
  # for a push at all. A missing remote is a better error than a real-looking
  # path.
  git -C "$_dest" remote remove origin
  # Empty on an unborn HEAD (a gatehouse repo created but never pushed to).
  # The harvest treats an empty base as "everything on the branch is new",
  # which is exactly right for a repo whose first commit is this build's.
  BASE_SHA+=( "$(git -C "$_dest" rev-parse --verify -q HEAD || true)" )
done
unset _repo_path _repo _dest
# ── END provisioning ─────────────────────────────────────────────────────


args=(
  run --rm
  # Per-build container name so concurrent builds never collide; the slug is
  # broker-validated kebab (branch/argv-safe).
  --name "$CONTAINER_NAME"
  --hostname "build-$SLUG"
  # keep-id: the calling res-* host uid appears INSIDE as uid 1000
  # ('resident'). Files the build writes to /home/resident are owned by the
  # res-* user on the host; its connect() to the broker socket carries the
  # res-* uid in SO_PEERCRED. Identity is the venue, not a credential.
  --userns "keep-id:uid=1000,gid=1000"
  --network "$NETWORK"
  # The worktree, READ-WRITE: the build commits its work to the loop/<slug>
  # branch here. (run-resident.sh mounts the same volume; a summon just does
  # not commit. The rw-ness is the volume's, called out here for the record.)
  -v "$HOME_VOL:/home/resident"
  # NO BROKER SOCKET. Deliberate deletion, 2026-08-06. The build seat used to
  # mount the broker's socket dir, which is how the 2026-08-05 session could
  # see `broker start-build` from inside a build — a build that starts builds,
  # nine slots deep, and (because [start_build].resident is global, BR-1) an
  # audit trail that cannot tell you it was not the resident. A build needs no
  # verb: its worktree is writable, its remote is the gatehouse, and its report
  # is its stdout, which the broker reaper already reads and posts. Removing
  # the mount kills the whole class rather than filtering it.
  -v "$CONFIG_DIR:/config:ro"
  # NO GATEHOUSE. The third deliberate deletion, 2026-08-13
  # (SPECS/2026-08-13-build-publish-path.md, architecture note 1b). This used
  # to be the "one writable path out": $GATEHOUSE mounted rw at /run/gatehouse,
  # with the session pushing into it. It cannot be that, because `--userns
  # keep-id` does not map supplementary groups — the `gatehouse` group does not
  # exist in here, so every in-container push wrote objects whose group was
  # whatever uid-ownership happened to produce, and the whole layout the
  # keyboard recipe builds (setgid, g+rwX, core.sharedRepository=group) was
  # being enforced against a container that could not see it.
  #
  # With this gone, NO writable path out of the container remains. /home/resident
  # is the seat's own home, /config is ro, /opt/* are ro. The product leaves by
  # the post-exit harvest at the bottom of this file, which runs out on the host
  # as res-<name> where the group and the safe.directory exemptions are real.
  # Do not add this mount back to "make pushing work": pushing working here was
  # the accident, not the mechanism.
  # The spec is fed on stdin; podman drops stdin without -i (always, unlike the
  # opt-in in run-resident.sh — a build with no spec is meaningless).
  -i
)

# NO /opt/house_memory. Also a deliberate deletion. The deployed copy is a
# read-only directory that is not a git repo, and mounting it is what led the
# blocked session to conclude the half of its spec that mattered "had nowhere
# to land". Under branch B the builder edits house_memory where it LIVES —
# ~/work/disjorn/harness/house_memory — and installing it is a keyboard step
# after merge, never the build's job.
: "${HOUSE_MEMORY:=}"  # retained only so the launcher's --setenv stays harmless

# The read-only repo mirror at /opt/disjorn. This was MISSING here while
# run-resident.sh has had it since WP-H1 — run-build.sh only ever mentioned
# RESIDENT_DISJORN_RO inside a comment copied from its sibling, so a build
# session had no /opt/disjorn at all. That is not cosmetic: /opt/disjorn is the
# container-side prefix `[residents.<r>.path_map]` maps for classify-diff, so a
# build could not have tier-classified its own diff. Added 2026-07-22.
#
# Same contract as run-resident.sh: the source MUST be a git-clean clone
# readable by res-* (/srv/disjorn-ro, refreshed after merges) and NEVER the
# live working tree — /home/plink is 0700 so rootless podman cannot mount it,
# and the working tree carries runtime data/ including the prod DB, which is a
# privacy wall, not an inconvenience.
if [ -n "${RESIDENT_DISJORN_RO:-}" ]; then
  [ -d "$RESIDENT_DISJORN_RO" ] || { echo "run-build: RESIDENT_DISJORN_RO not a dir: $RESIDENT_DISJORN_RO" >&2; exit 1; }
  args+=( -v "$RESIDENT_DISJORN_RO:/opt/disjorn:ro" )
fi

# ── BEGIN spine mount block ──────────────────────────────────────────────
# Byte-identical in run-resident.sh and run-build.sh; a test asserts that
# (harness/cc/tests/test_run_wrappers.py::test_spine_block_is_identical_in_both_wrappers).
# Edit one, paste into the other.
#
# BOTH SEATS AGAIN as of 2026-08-12, and the history matters because this block
# has already been deleted once. Branch B (2026-08-06) took it out of
# run-build.sh on two grounds: that a build session is a tool rather than a
# resident, and that `assemble_for_seat("build")` raised "no kernel entry
# visible to seat 'build'" against the spine it was tried on. The second ground
# was an observation about CLAUDETTE's spine, every entry of which is
# `seats: [resident]`, generalised into a claim about the seat.
#
# GABLE's spine has carried an explicit build seat since the seat-split
# (SPECS/2026-07-22-gable-spine-ro-cutover-seat-split.md), applied and verified
# live 07-23: resident 7 entries, build 5, stamped `(seat: build)`. His ruling
# as review owner is that baking the operational set for a detached build is
# "correct, not a compromise" — a build has no retrieval loop, so an
# operational entry that is not baked never arrives at all, and "the build seat
# does load-bearing work with walls it's never read."
#
# The mount is restored for both seats by
# SPECS/2026-08-08-gable-build-lane-provisioning.md (confirmed by plink,
# #custodian seq 1008). Branch B's OTHER two deletions from run-build.sh — the
# broker socket and house_memory — STAND; only the spine came back. See
# harness/cc/BUILD-SEAT-CONTRACT.md before touching any of the three.
#
# PROTECTION BY PLACEMENT for the resident's SPINE — the directory
# house_memory/bootstrap.py assembles into ~/.claude/CLAUDE.md at the start
# of EVERY session. The spine is the resident's kernel. AGENTHOOD.md rules
# that a resident's own code and prompt are always Tier 2 (a human reviews
# every change), and bootstrap.py's docstring assumes a spine edit arrives
# "witnessed, merged".
#
# Neither holds if the spine the container loads sits in the resident's own
# read-write home volume: the resident rewrites its kernel directly and the
# next session loads it — no diff for the WP-H4 classifier to tier, no
# #custodian post, no human. The classifier sees SUBMITTED diffs; it cannot
# see a direct write. Only placement can.
#
# So: mount a plink-owned mirror READ-ONLY at /opt/spine, and have
# RESIDENT_SPINE_DIR (read by bootstrap.py, set in the /config env file)
# point there. Three independent walls, none trusting the others:
#   1. host ownership — the mirror is plink:plink 0755/0644 and the res-*
#      uid is neither owner nor group;
#   2. the `:ro` bind — a write is EROFS even if (1) were wrong;
#   3. the refusal below — we will not launch at all if the source is
#      writable by the uid we are running as. That is the check that
#      catches a cutover mis-pointed back at the home volume.
#
# Opt-in per resident, HOST-side, exactly like RESIDENT_DISJORN_RO: set
# RESIDENT_SPINE_HOST in the unit's Environment=. UNSET adds no mount and
# no flag — byte-for-byte today's podman invocation — so shipping this
# cannot regress a live summon. Mounting alone still changes nothing about
# which spine loads; the cutover is a separate deliberate line in the env
# file (config-template/README.md § Spine placement).
#
# MOUNTING IS NOT THE CUTOVER, and for the BUILD seat that has a second edge.
# The build seat's kernel today is the file run-build.sh COPIES to
# ~/.claude/CLAUDE.md (build-kernel.md), and nothing in [start_build].session_argv
# runs bootstrap.py — so a mounted spine sits there unread until plink both
# sets RESIDENT_SPINE_DIR=/opt/spine in the build seat's /config env file AND
# adds the bootstrap call to session_argv. Those two move together, because
# bootstrap.py WRITES ~/.claude/CLAUDE.md and would otherwise overwrite the
# copied task kernel with no one having chosen that. Both are plink's
# deliberate step, not this wrapper's: harness/cc/BUILD-SEAT-CONTRACT.md
# § Kernel carries the ordering.
#
# The source MUST be the res-readable mirror (/srv/disjorn-spine/<name>,
# published by harness/keyboard/06-spine-mirror.sh after plink approves a
# spine change), NEVER the canonical copy under /home/plink: that tree is
# 0700 and rootless podman cannot mount it. Do not "fix" that by loosening
# /home/plink/bots/<name>/spine — that directory is the authorization
# surface itself. Copy outward; never open inward.
if [ -n "${RESIDENT_SPINE_HOST:-}" ]; then
  _spine_tag="$(basename "$0" .sh)"
  [ -d "$RESIDENT_SPINE_HOST" ] || { echo "$_spine_tag: RESIDENT_SPINE_HOST not a dir: $RESIDENT_SPINE_HOST" >&2; exit 1; }
  # Fail CLOSED, not quietly: if this uid can write the spine source, the
  # read-only mount is theatre (the resident can edit the host path
  # directly, outside the container, and the next session loads it). Refuse
  # the launch and say exactly why. `-writable` is access(2) as the calling
  # uid, so it accounts for ownership, group, and ACLs — not just mode bits.
  _spine_writable="$(find "$RESIDENT_SPINE_HOST" -maxdepth 1 -writable -print -quit 2>/dev/null)"
  if [ -n "$_spine_writable" ]; then
    echo "$_spine_tag: REFUSING TO LAUNCH: spine source is WRITABLE by this uid ($(id -un)): $_spine_writable" >&2
    echo "$_spine_tag: the spine is the kernel and must be resident-unwritable. Point RESIDENT_SPINE_HOST at the plink-owned mirror (/srv/disjorn-spine/<name>, see harness/keyboard/06-spine-mirror.sh) — do NOT loosen the canonical spine to make this pass." >&2
    exit 1
  fi
  unset _spine_writable _spine_tag
  args+=( -v "$RESIDENT_SPINE_HOST:/opt/spine:ro" )
fi
# ── END spine mount block ────────────────────────────────────────────────

ENV_FILE="$CONFIG_DIR/env"

# THE ROUTING LINE (see the credential block's own § ROUTING BY SEAT). This is
# the BUILD seat: per the credential-routing spec, build and agent loops run on
# plink's Max account, so there is NO metered fallback here. A build that finds
# only an API key does not quietly bill it — it refuses to launch. Deliberately
# OUTSIDE the byte-identical block below — it is the one credential decision
# that differs by seat, and it differs because of which wrapper launched, never
# because of /config.
_seat_metered_fallback=refuse

# ── BEGIN credential block ───────────────────────────────────────────────
# Byte-identical in run-resident.sh and run-build.sh; a test asserts that
# (harness/cc/tests/test_run_wrappers.py::test_credential_block_is_identical_in_both_wrappers).
# Edit one, paste into the other.
#
# WHICH CREDENTIAL. Two are accepted, from the env file and NOWHERE else
# (this script deliberately ignores its own environment, so a stray key in a
# systemd unit cannot become a session's identity):
#   CLAUDE_CODE_OAUTH_TOKEN  a long-lived OAuth token minted by
#                            `claude setup-token`; bills plink's Claude Max
#                            SUBSCRIPTION. Preferred.
#   ANTHROPIC_API_KEY        a metered API key. Fallback — but only for a seat
#                            allowed to spend it; see § ROUTING BY SEAT below.
# If both are present the OAuth token wins and the API key is NOT passed —
# "exactly one credential in the container" is the invariant. If neither is
# present we warn loudly and pass none: fail loud, never fail over silently.
#
# ROUTING BY SEAT (SPECS/2026-08-05-credential-routing-and-halt-protocol.md §1
# and §3; SPECS/2026-08-08-gable-build-lane-provisioning.md, architecture note
# 3). The routing table is the seat split: conversational seats run on metered
# per-seat API keys, build/agent loops run on plink's Max account. So the
# API-key fallback is NOT universal. Each wrapper sets `_seat_metered_fallback`
# immediately above this block — `allow` for the chat seat, `refuse` for the
# build seat — and a refusing seat that finds ONLY an API key does not launch.
#
# It refuses rather than warning-and-continuing because the halt protocol says
# so in as many words: no silent key-fallback. A loop that quietly fails over
# from Max to the metered key has converted "your account said not now" into
# "spend your API money instead", which is precisely the decision plink
# reserved for himself (seqs 683/697: "I need to look at the account before
# restarting any work that ate the whole budget"). The full fix is the
# broker-side injecting proxy in that spec, where neither credential is inside
# any container at all; until it lands this refusal holds the same line at the
# one place a build can spend.
#
# The one line that differs by seat lives OUTSIDE this block on purpose, so the
# block stays byte-identical and cannot drift between the two wrappers.
#
# HOW IT IS PASSED (and why not the obvious way).
#   * The value NEVER appears in argv. `podman --env VAR=value` would put a
#     credential for plink's whole Claude account into the process table,
#     readable by any process on the host via /proc/*/cmdline. We use the
#     NAME-ONLY form `--env VAR`, which tells podman "take VAR from my own
#     environment" — argv carries the name only.
#   * The remaining env-file vars (BROKER_DISABLE, …) still go through
#     podman's own --env-file parser, so their semantics never drift from
#     podman's. But podman offers no way to drop a var an --env-file sets:
#     `--unsetenv` does not touch env-file vars, and `--env VAR=` only
#     blanks it (verified against podman 5.4.2). So the file podman reads is
#     a FILTERED copy with both credential lines removed. That copy is
#     created 0600, opened on fd 9, and UNLINKED before exec — it holds no
#     credential and does not outlive the launch. podman does not pass extra
#     fds to the container (no --preserve-fds), so fd 9 stops here.
#   * Value parsing matches podman's env-file semantics exactly: everything
#     after the first "=" is taken literally — no quote stripping, no
#     trimming (verified against podman 5.4.2). Do not quote the token.
#     The bare `NAME` (inherit-from-environment) env-file form is NOT
#     supported for credentials; write NAME=value.
#   * The env FILE itself is masked inside the container: /config is mounted
#     ro, and the resident could otherwise just `cat /config/env` and read
#     the credential out of it (settings.json denies Read(//config/env), but
#     Bash(cat:*) and Bash(python3:*) are allowed, so that deny is hygiene,
#     not a wall). We bind /dev/null over /config/env, so the file reads
#     empty from inside. This removes the FILE copy only — it does NOT and
#     cannot hide the credential from the session itself, which necessarily
#     has it in its own environment (/proc/self/environ). See
#     config-template/README.md § Security note.
#     Escape hatch for debugging: RESIDENT_MASK_ENV=0.
_tag="$(basename "$0" .sh)"
_cred_name=""
_cred_value=""
_oauth_value=""
_apikey_value=""

_read_env_var() {  # _read_env_var NAME FILE -> value on stdout (may be empty)
  local _line
  _line="$(grep -E "^[[:space:]]*$1=" "$2" | tail -n1)" || true
  [ -n "$_line" ] && printf '%s' "${_line#*=}"
  return 0
}

if [ -f "$ENV_FILE" ]; then
  _oauth_value="$(_read_env_var CLAUDE_CODE_OAUTH_TOKEN "$ENV_FILE")"
  _apikey_value="$(_read_env_var ANTHROPIC_API_KEY "$ENV_FILE")"

  if [ -n "$_oauth_value" ]; then
    _cred_name="CLAUDE_CODE_OAUTH_TOKEN"
    _cred_value="$_oauth_value"
    if [ -n "$_apikey_value" ]; then
      echo "$_tag: WARNING $ENV_FILE sets BOTH CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY; using CLAUDE_CODE_OAUTH_TOKEN (Max subscription), NOT passing ANTHROPIC_API_KEY into the container" >&2
    fi
  elif [ -n "$_apikey_value" ]; then
    if [ "${_seat_metered_fallback:-allow}" = "refuse" ]; then
      echo "$_tag: REFUSING TO LAUNCH: $ENV_FILE offers ANTHROPIC_API_KEY only, and this seat routes to the Max account — it must never silently spend the metered key. Put a CLAUDE_CODE_OAUTH_TOKEN (\`claude setup-token\`) in $ENV_FILE, or change the route deliberately at the keyboard. No silent key-fallback: SPECS/2026-08-05-credential-routing-and-halt-protocol.md §3." >&2
      exit 1
    fi
    _cred_name="ANTHROPIC_API_KEY"
    _cred_value="$_apikey_value"
  fi

  if [ -n "$_cred_name" ]; then
    echo "$_tag: auth: $_cred_name from $ENV_FILE" >&2
  else
    echo "$_tag: WARNING no credential in $ENV_FILE (expected CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY) — CC sessions will fail to authenticate" >&2
  fi

  # Filtered copy: every line EXCEPT the two credential assignments (and
  # their bare inherit form). Created 0600, then unlinked — podman reads it
  # through the inherited fd.
  _filtered="$(mktemp "${TMPDIR:-/tmp}/${_tag}-env.XXXXXXXX")"
  chmod 0600 "$_filtered"
  grep -vE '^[[:space:]]*(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY)([[:space:]]*=|[[:space:]]*$)' \
    "$ENV_FILE" > "$_filtered" || true
  exec 9<"$_filtered"
  rm -f "$_filtered"
  args+=( --env-file /dev/fd/9 )

  if [ "${RESIDENT_MASK_ENV:-1}" != "0" ]; then
    args+=( -v "/dev/null:/config/env:ro" )
  else
    echo "$_tag: WARNING RESIDENT_MASK_ENV=0 — /config/env is readable from inside the container; the session can read the credential out of the file" >&2
  fi
else
  echo "$_tag: WARNING env file absent: $ENV_FILE (no CLAUDE_CODE_OAUTH_TOKEN, no ANTHROPIC_API_KEY) — CC sessions will fail to authenticate" >&2
fi

# Hand the winner to podman by NAME only. Unset the loser in our own env so
# nothing inherited can shadow the decision made above.
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY
if [ -n "$_cred_name" ]; then
  export "$_cred_name=$_cred_value"
  args+=( --env "$_cred_name" )
fi
unset _cred_value _oauth_value _apikey_value
# ── END credential block ─────────────────────────────────────────────────

# SEAT (spec 2026-07-22 seat-split). This wrapper is the BUILD seat: a
# detached build session loads the OPERATIONAL set only (00-nonnegotiables,
# 10-people, 20-load-bearing-walls, 30-build-rhythm, 40-cautions) and NEVER
# biography (05-bearings, 50-genesis). "House knowledge travels, biography
# doesn't." bootstrap.py reads RESIDENT_SEAT inside the container and, for the
# build seat, BAKES every entry the seat loads — a detached build has no
# retrieval loop, so an un-baked operational entry would simply be absent.
# Passed AFTER the credential block so the wrapper's seat wins over any
# /config env-file value: the seat is a property of WHICH wrapper launched.
# Deliberately NOT inside a byte-identical block — one of the two lines that
# MUST differ from run-resident.sh (the other is _seat_metered_fallback).
#
# It is passed unconditionally and always has been, INCLUDING while the spine
# was deleted from this seat: RESIDENT_SEAT is the seat's name, not a claim
# that a spine is loaded. It only does anything when bootstrap.py runs, which
# [start_build].session_argv does not currently call — see the BUILD_KERNEL
# note near the top and BUILD-SEAT-CONTRACT.md § Kernel.
args+=( -e "RESIDENT_SEAT=build" )

if [ -n "${RESIDENT_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of extra flags
  args+=( ${RESIDENT_PODMAN_EXTRA} )
fi

# ── BEGIN dependency preflight ───────────────────────────────────────────
# REFUSE BEFORE SPAWN IF THE SEAT CANNOT RUN A TEST.
#
# Proposed by Gable on 2026-08-12 (#custodian seq 1044) and not built until
# 2026-08-14, which cost a third build its test run. The disease, three times:
#   08-06  no pytest in the image at all
#   08-12  same, one layer wider — a zero-diff build
#   08-14  no aiosqlite; 22 tests written, 0 executed, held at review
# Every one of those sessions behaved correctly. It wrote the tests, found no
# runner, refused to pip-install its way to a green result, and said so. The
# cost each time was a slot, an hour, and a diff a human had to hold at arm's
# length — for a condition knowable in under a second, before anything started.
#
# WHY THIS IS NOT THE SAME CHECK AS tests/test_image_deps.py. That test reads
# the REPO and asserts the Containerfile installs what pyproject and
# requirements.txt declare. It cannot see the image that is about to run. This
# sees only the running image and cannot see the repo. The gap between them —
# a correct Containerfile committed and the image never rebuilt — is exactly
# the shape of every "committed versus deployed" failure this project has paid
# for, including 08-14's, where the fix was one commit old and the seat still
# lacked the module.
#
# A CANARY, NOT A MANIFEST, and it says so out loud rather than implying
# completeness it does not have: one sentinel import per stack a spec might
# touch. The drift test guards the list; this guards the image.
PREFLIGHT_IMPORTS="${RESIDENT_PREFLIGHT_IMPORTS:-pytest aiosqlite fastapi httpx argon2 chromadb voyageai}"
# EX_CONFIG (78). The broker refunds the build slot on exactly this code — a
# seat that was never fit is a build that never started, so it must not cost an
# attempt. Any other nonzero exit still burns the slot: that is a build that ran
# and failed.
PREFLIGHT_EXIT=78
if [ "${RESIDENT_PREFLIGHT:-1}" = "1" ]; then
  # `import importlib.util` — NOT a bare `import importlib`. The submodule is
  # not auto-bound, so the bare form raises AttributeError at find_spec and the
  # probe dies instead of answering. It did exactly that on first run, and the
  # old fail-open swallowed it: a crashed probe read as "nothing missing" and
  # the build launched. A check that cannot fail loudly is not a check, which
  # is the whole disease this thing was built to treat.
  _pf_py='import importlib.util, sys
missing=[m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]
print(" ".join(missing))
sys.exit(1 if missing else 0)'
  # Same image, same uid, same userns as the real run — a preflight in a
  # different context would be testing something other than the seat.
  # `if` and not a bare assignment: under `set -e` an assignment whose command
  # substitution exits non-zero kills the script THERE, so `_pf_rc=$?` on the
  # next line never runs and the refusal never prints. That is exactly what it
  # did on first run — the wrapper died with the probe's exit 1, silently, and
  # the informative message I had written was unreachable code.
  if _pf_missing="$(podman run --rm --userns "keep-id:uid=1000,gid=1000" \
      "$IMAGE" python3 -c "$_pf_py" $PREFLIGHT_IMPORTS 2>/dev/null)"; then
    _pf_rc=0
  else
    _pf_rc=$?
  fi
  # THREE OUTCOMES, and the third is the one that used to be invisible:
  #   0  every module imported            -> launch
  #   1  the probe ran and named the gaps -> refuse, listing them
  #   *  the probe itself could not run   -> refuse, saying so
  # Treating anything-but-1 as success is how a broken probe becomes a silent
  # pass. If the seat cannot even be interrogated, that is not a green light.
  if [ "$_pf_rc" -eq 1 ]; then
    echo "PREFLIGHT-FAILED: the build image cannot import:${_pf_missing}" >&2
    echo "run-build: refusing to start — a session in this seat would write tests it cannot run." >&2
    echo "run-build: image=$IMAGE. Fix: rebuild it with harness/keyboard/07-resident-image.sh, then retry." >&2
    echo "run-build: no build slot has been spent." >&2
    exit "$PREFLIGHT_EXIT"
  elif [ "$_pf_rc" -ne 0 ]; then
    echo "PREFLIGHT-FAILED: the dependency probe could not run in $IMAGE (exit $_pf_rc)." >&2
    echo "run-build: refusing rather than assuming the seat is fit — an uninterrogable seat is not a passing one." >&2
    echo "run-build: no build slot has been spent." >&2
    exit "$PREFLIGHT_EXIT"
  fi
  unset _pf_py _pf_missing _pf_rc
fi
# ── END dependency preflight ─────────────────────────────────────────────

# ── BEGIN container reaper block ─────────────────────────────────────────
# NO LONGER BYTE-IDENTICAL WITH run-resident.sh, as of 2026-08-13. It was,
# until this seat stopped `exec`ing podman: run-resident.sh still hands its
# process to podman and needs the watchdog alone, while this wrapper must
# OUTLIVE its container to run the harvest and therefore needs the watchdog
# AND a trap. The divergence is a contract, not drift — the test that used to
# assert byte-equality now asserts each wrapper's own form
# (harness/cc/tests/test_run_wrappers.py::test_reaper_blocks_diverge_by_contract).
# Keep the two mechanisms named below in sync in SUBSTANCE (reap by cid,
# `rm -f -t 0 --ignore`, never by name); do not paste this block back over
# run-resident.sh's.
#
# THE GAP THIS CLOSES. The container is NOT this process's child. Rootless
# `podman run` hands it to conmon, which is reparented away, so killing the
# podman CLIENT leaves the container running. Measured on podman 5.4.2:
# SIGKILL the client and `podman ps` still shows the container Up
# (tests/test_container.sh check 14a asserts that baseline, so if a future
# podman fixes it upstream we find out instead of quietly duplicating it).
#
# That matters because two supervisors kill this wrapper and expect the
# session to stop with it:
#   * residency/launcher.py's pre-act model gate, when the resolved model
#     does not match the pin — it refuses the session and kills the process
#     it spawned;
#   * brokerd.py's build reaper, at start_build.timeout_sec.
# Their channel guarantee holds regardless (nothing a refused or timed-out
# session produces is ever read or posted). What did NOT stop were the SIDE
# EFFECTS: a refused session inside a still-running container keeps writing
# to the home volume and keeps calling the broker. At the `init` stage that
# window is near-zero, but a mid-session refusal can have tool calls already
# in flight and more still to come.
#
# WHY NOT A SIGNAL TRAP *ALONE*. Both supervisors use Python's `proc.kill()`,
# which is SIGKILL, and no trap runs on SIGKILL — a trap-based reaper would
# look closed without being closed. That is why the watchdog exists and why it
# is still the primary mechanism here.
#
# WHAT WORKS FOR SIGKILL. A watchdog sibling that waits for THIS pid to
# disappear and then takes the container down. It survives the wrapper's death
# because a single-pid kill does not touch it, and it covers every exit path —
# SIGKILL, SIGTERM, SIGINT, crash, and normal completion (where the container
# is already gone and the reap is a no-op).
#
# AND WHY A TRAP AS WELL, from 2026-08-13 ([1175], spec architecture note 1c).
# The launch tail no longer `exec`s: this wrapper stays alive as podman's
# PARENT so it can harvest after the container exits. That is a new hazard, not
# just a new shape. A parent killed by SIGTERM/SIGINT used to take podman with
# it because podman WAS the process; now podman is a child that outlives it,
# and a container still running while the NEXT launch `rm -rf`s ~/work/<repo>
# underneath it is the exact interleaving the quarantine clause is trying to
# stop from ever mattering. So: the trap reaps on EXIT/INT/TERM — promptly,
# with the podman client killed too — and the watchdog still covers the SIGKILL
# the trap cannot see. Two mechanisms for two different signals, not two
# mechanisms for one job; both are idempotent (`--ignore`), so a double reap is
# a no-op rather than a disagreement.
#
# The trap is also why podman is launched with `&` + `wait` rather than as a
# plain foreground command: bash DEFERS a trap until the current foreground
# command finishes, so a SIGTERM arriving mid-build would sit unhandled for the
# rest of the session. `wait` is interruptible; the trap fires at once. Stdin
# is preserved explicitly (`<&0`) because bash otherwise redirects an async
# command's stdin from /dev/null — and stdin is the spec.
#
# IT REAPS BY CONTAINER ID, NOT BY NAME, and that distinction is the whole
# correctness of this block. Container names are per-resident and REUSED
# every summon ("resident-cc-gable"). A watchdog that reaped by name would,
# in the up-to-one-poll window after its own wrapper exits, kill the NEXT
# summon's container instead of its own — turning a safety feature into an
# intermittent killer of healthy sessions. (This is not hypothetical: the
# first version of this block did exactly that, and check 14 caught it.)
# --cidfile pins the identity, so the watchdog can only ever reap the one
# container it was started for.
#
# `rm -f -t 0`, chosen deliberately over `stop`:
#   * -t 0 => SIGKILL now, no grace period. A grace period is time in which
#     a session we have already decided to refuse keeps calling tools.
#     Nothing in-container needs flushing: /home/resident is a bind mount,
#     so completed writes are already on the host, and the half-finished
#     work is exactly what must not complete.
#   * --ignore => a container that already exited is not an error, so the
#     watchdog can never turn a clean run into a failure.
#
# NOT ARMED WHEN DETACHED. `RESIDENT_PODMAN_EXTRA=-d` means "start the
# container and return"; the wrapper exiting IS the success path there, and
# a watchdog would kill the container it just started. Detached callers own
# their container's lifetime.
#
# The watchdog's stdio goes to /dev/null: it must not hold the wrapper's
# stdout/stderr pipes open, because launcher.py reads those to EOF and an
# inherited pipe would keep EOF from ever arriving.
#
# Escape hatch for debugging a container that dies too fast to inspect:
# RESIDENT_REAP=0 (warns loudly).
_reap_tag="$(basename "$0" .sh)"
# DETACHED is read again by the launch tail (a detached run keeps `exec
# podman`: no trap, no harvest, the caller owns the container). One variable,
# computed once — the reaper and the tail must never disagree about whether
# this run is detached.
DETACHED=0
for _w in ${RESIDENT_PODMAN_EXTRA:-}; do
  case "$_w" in -d|--detach|--detach=true) DETACHED=1 ;; esac
done
_reap_cid=""
if [ "${RESIDENT_REAP:-1}" = "0" ]; then
  echo "$_reap_tag: WARNING RESIDENT_REAP=0 — container $CONTAINER_NAME will OUTLIVE this wrapper if the wrapper is killed; a refused or timed-out session keeps running inside it, writing into $WORK while the next launch deletes it" >&2
elif [ "$DETACHED" = "1" ]; then
  : # detached by request: the caller owns the container's lifetime
else
  # A private DIRECTORY, not `mktemp -u`: podman refuses to start if the
  # cidfile already exists, so an unlinked-name guess is a race that would
  # turn into a failed summon. mktemp -d cannot collide.
  _reap_ciddir="$(mktemp -d "${TMPDIR:-/tmp}/${_reap_tag}-cid.XXXXXXXX")"
  _reap_cid="$_reap_ciddir/cid"
  args+=( --cidfile "$_reap_cid" )
  _reap_pid=$$
  (
    while kill -0 "$_reap_pid" 2>/dev/null; do sleep 0.25; done
    # The container may still be being created as we die; give the cidfile a
    # moment to appear before concluding there is nothing to reap.
    for _ in $(seq 20); do [ -s "$_reap_cid" ] && break; sleep 0.1; done
    if [ -s "$_reap_cid" ]; then
      podman rm -f -t 0 --ignore "$(cat "$_reap_cid")"
    fi
    rm -rf "$_reap_ciddir"
  ) >/dev/null 2>&1 </dev/null &

  # The trap's half. Same cid, same flags, same idempotence — it just gets
  # there first on the signals a trap can actually see. Output is discarded so
  # a reap message can never land in the middle of the machine-readable
  # PUBLISHED/NO-COMMITS lines the broker reaper parses; `|| true` because a
  # trap that fails under `set -e` would replace a real exit status with its
  # own. _podman_pid is empty until the tail starts podman.
  _build_reap() {
    if [ -n "${_podman_pid:-}" ]; then
      kill "$_podman_pid" 2>/dev/null || true
    fi
    if [ -n "$_reap_cid" ] && [ -s "$_reap_cid" ]; then
      podman rm -f -t 0 --ignore "$(cat "$_reap_cid")" >/dev/null 2>&1 || true
    fi
    return 0
  }
  # EXIT covers the ordinary path and every `exit` below; INT/TERM re-exit with
  # the conventional 128+signal so the broker still sees "killed", not "clean".
  trap '_build_reap' EXIT
  trap '_build_reap; exit 130' INT
  trap '_build_reap; exit 143' TERM
fi
unset _reap_tag _w
# ── END container reaper block ───────────────────────────────────────────

args+=( "$IMAGE" )
# Everything after <resident> <slug> is forwarded verbatim as the container
# command. This carries the build session_argv AND the WP-L5 model pin: the
# broker appends `--model <id>` to the argv, so it arrives here in "$@" and
# rides into the container command unchanged — identical to run-resident.sh.
[ "$#" -gt 0 ] && args+=( "$@" )


# ── BEGIN launch + post-exit harvest ─────────────────────────────────────
# DETACHED KEEPS THE OLD SHAPE. `RESIDENT_PODMAN_EXTRA=-d` means "start the
# container and return"; there is nothing to wait for and nothing to harvest
# yet, so exec and be done. Harvesting here would measure a container that has
# not written a line, and the trap would kill what it just started.
if [ "$DETACHED" = "1" ]; then
  exec podman "${args[@]}"
fi

# FOREGROUND, NOT exec (2026-08-13). The exec was load-bearing for years — same
# PID, same stdin, same exit status, no extra shell — and giving it up costs
# something real, so it was given up for something real: with the gatehouse
# unmounted, THIS is the only process that can publish the build's work, and it
# has to still be alive when the container stops. `&` + `wait` rather than a
# plain foreground command so the reaper block's trap is not deferred; `<&0` so
# the spec on stdin survives the async redirect bash would otherwise apply.
podman "${args[@]}" <&0 &
_podman_pid=$!
_container_rc=0
wait "$_podman_pid" || _container_rc=$?
_podman_pid=""

# HARVEST ONLY AFTER A CLEAN EXIT. A nonzero container is a build that failed,
# refused, or crashed; whatever sits on its branch is not a product and does not
# get published under a line that says it is. The clones are LEFT WHERE THEY
# ARE — the quarantine clause above is what protects them at the next launch,
# and it protects them by measuring the gatehouse rather than by trusting this
# path to have written a marker.
if [ "$_container_rc" -ne 0 ]; then
  echo "HARVEST-SKIPPED container exited $_container_rc"
  echo "run-build: container exited $_container_rc — not publishing. The workspace clones under $WORK are left in place; the next launch will quarantine any that hold unharvested commits." >&2
  exit "$_container_rc"
fi

# THE HARVEST. One line per entitled repo, always, on stdout — this is the ONLY
# thing the broker reaper's done-banner is built from (spec architecture note
# 3: no separate verification path, because the harvest IS the verification and
# two mechanisms can disagree). Three outcomes and no fourth:
#
#   PUBLISHED <repo>.git <sha>   the gatehouse was rev-parsed AFTER the push
#                                and holds exactly the sha we measured here.
#   NO-COMMITS <repo>.git        the branch never moved past the clone point.
#                                The honest line for a zero-commit build; the
#                                banner it produces says so instead of "on the
#                                branch for review" about a phantom branch.
#   PUBLISH-FAILED <repo>.git …  the git error, verbatim, flattened to one line.
#
# ABSENCE of all three is also a signal, and the one that matters most: a
# wrapper killed at start_build.timeout_sec dies before this loop, so the
# reaper sees no lines and declares FAILED. Silence is never read as success.
#
# NO FORCE, EVER. Not --force, not --force-with-lease, not +refs/. A
# non-fast-forward here means the gatehouse branch moved under us — someone
# else's rescue push, or a re-run of the same slug — and the only correct
# response is to print git's own refusal and fail. The wrapper is not entitled
# to decide whose commits lose.
_publish_failed=0
for _i in "${!ENTITLED[@]}"; do
  _repo="${ENTITLED[$_i]}"
  _base="${BASE_SHA[$_i]}"
  _dest="$WORK/$_repo"
  _bare="$GATEHOUSE/$_repo.git"

  _head="$(git -C "$_dest" rev-parse --verify -q "refs/heads/$BRANCH" 2>/dev/null || true)"
  if [ -z "$_head" ]; then
    # The session deleted or renamed the branch we made for it. Nothing to
    # publish and nothing to invent.
    echo "NO-COMMITS $_repo.git"
    continue
  fi
  # "Beyond the clone point", counted rather than compared: a session that
  # rebased or reset still gets an honest answer, and an empty base (unborn
  # HEAD at clone time) means every commit on the branch is new.
  if [ -n "$_base" ]; then
    _new="$(git -C "$_dest" rev-list --count "$_base..$BRANCH" 2>/dev/null || echo 0)"
  else
    _new="$(git -C "$_dest" rev-list --count "$BRANCH" 2>/dev/null || echo 0)"
  fi
  if [ "$_new" -eq 0 ]; then
    echo "NO-COMMITS $_repo.git"
    continue
  fi

  # Push the SHA we just measured, not `HEAD`: the line we are about to print
  # names a sha, and pushing a symbolic ref would let the thing published and
  # the thing reported drift apart between the two commands.
  _err="$(git -C "$_dest" push "$_bare" "$_head:refs/heads/$BRANCH" 2>&1)" || {
    echo "PUBLISH-FAILED $_repo.git $(printf '%s' "$_err" | tr '\n\r\t' '   ' | tr -s ' ')"
    _publish_failed=1
    continue
  }
  # MEASURE IN THE GATEHOUSE, not in the clone. `git push` exiting 0 is a claim
  # about a transport; the banner is a claim about what a human will find when
  # they fetch. Only the second one is worth printing.
  _landed="$(git -C "$_bare" rev-parse --verify -q "refs/heads/$BRANCH" 2>/dev/null || true)"
  if [ "$_landed" = "$_head" ]; then
    echo "PUBLISHED $_repo.git $_head"
  else
    echo "PUBLISH-FAILED $_repo.git push reported success but $_bare refs/heads/$BRANCH is '${_landed:-absent}', not $_head"
    _publish_failed=1
  fi
done
unset _i _repo _base _dest _bare _head _new _err _landed

# The wrapper's own status. Zero only when the container was clean AND every
# entitled repo produced a PUBLISHED or a NO-COMMITS line: a build whose work
# did not reach the gatehouse is a failed build, whatever the session said
# about itself in its JSON.
if [ "$_publish_failed" -ne 0 ]; then
  exit 1
fi
exit 0
# ── END launch + post-exit harvest ───────────────────────────────────────
