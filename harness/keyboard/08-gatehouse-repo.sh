#!/usr/bin/env bash
# 08-gatehouse-repo.sh — create ONE gatehouse bare repo with the whole
# permission recipe applied AT CREATION, and verify it FROM THE RESIDENT SEAT.
#
# RUN BY: plink, with sudo.
#   sudo bash 08-gatehouse-repo.sh create gable gable
#   sudo bash 08-gatehouse-repo.sh verify gable gable
#            └ mode           └ repo  └ resident(s) that push to it
#
# Idempotent: `create` on a repo that already exists re-applies the recipe and
# says so; it never touches refs, objects or hooks. `create` always finishes by
# running `verify`, and `verify` trusts nothing `create` just did.
#
# WHY A SCRIPT AND NOT SIX COMMANDS IN A RUNBOOK
# ----------------------------------------------
# Because the six commands were a runbook, and the repo that got them one at a
# time is the one that had to be repaired afterwards. A bare repo shared by two
# uids needs several properties TOGETHER, and any one of them missing produces
# the same symptom weeks later: one object file carries the creating process's
# private group, and the other uid gets "Permission denied" on a fetch it has
# done a hundred times. Repairing after the fact fixes the files that exist and
# not the ones git writes next.
#
# SPECS/2026-08-08-gable-build-lane-provisioning.md, architecture note 1:
# "with the full recipe AT CREATION, not repaired after". This script is that
# sentence, and `verify` is architecture note 5's wrong-group file check.
#
# THE RECIPE, and what each part is actually for
# ---------------------------------------------
#   owner = the broker user      the broker fetches, classifies and merges out
#                                of this repo; it must own what it reads.
#   group = gatehouse            the shared name. NOT the resident's private
#                                group and NOT the broker's: a group that means
#                                "may use the gatehouse" can be granted and
#                                revoked without touching either identity.
#   setgid on every directory    new files inherit the DIRECTORY's group rather
#                                than the creating process's primary group.
#                                This is the one that cannot be retrofitted:
#                                without it every object a resident writes
#                                lands in group res-<name>, and the broker
#                                cannot read it.
#   g+rwX                        the group may read, write and traverse. X, not
#                                x: directories become traversable, plain files
#                                do not become executable.
#   core.sharedRepository=group  git's own half. git writes objects and refs
#                                 0444/0644 unless told the repo is shared; with
#                                `group` it chmods what it writes to be
#                                group-writable and setgids directories it
#                                creates itself.
#   safe.directory (see below)   git's ownership check, which this layout trips
#                                by design.
# The mode bits and the git config are BOTH required and neither implies the
# other: the first governs what the kernel does, the second what git does to
# files after creating them.
#
# THE OWNERSHIP CHECK, WHICH IS NOT OPTIONAL HERE
# ----------------------------------------------
# Since CVE-2022-24765, git refuses to operate on a repository whose directory
# is owned by a different user than the one running git ("detected dubious
# ownership"). A gatehouse repo owned by the broker user and pushed to by
# res-<name> trips that on purpose: different owner is the whole design. So the
# recipe includes an explicit `safe.directory` entry per repo, in the SYSTEM
# gitconfig (/etc/gitconfig, root-owned) rather than in a resident's own
# ~/.gitconfig — protection by placement, same as every other switch in this
# house: the exemption lives where a resident cannot widen it.
#
# It is per-REPO and never `*`. `safe.directory=*` would exempt every path on
# the box, including a repo a resident had planted somewhere, which is the exact
# hazard the check exists for.
#
# WHAT THIS GRANTS, STATED PLAINLY. A resident added to the `gatehouse` group
# can write every byte of every repo in the gatehouse — including refs. That is
# already true through the mount (run-build.sh bind-mounts the gatehouse rw; it
# is the one writable path out of a build container), so this changes no wall —
# it makes the existing one work for a second uid. The wall that matters is
# unchanged: these are BARE repos, so a push deploys nothing and merges nothing,
# and a human reads every diff. Branch-namespace enforcement in a pre-receive
# hook is specified in harness/cc/MERGE-CONTRACT.md and is NOT built; until it
# is, "a resident cannot move gatehouse main" is a fact about nobody having
# tried, not a wall. See harness/cc/BUILD-SEAT-CONTRACT.md § Exit.
#
# Env overrides (tests and staging use these; production uses the defaults):
#   GATEHOUSE_DIR     default /var/lib/disjorn-broker/gatehouse
#   GATEHOUSE_GROUP   default gatehouse
#   BROKER_USER       default plink — the uid the broker runs as. WP-A1 gives
#                     the broker its own uid; when it lands, change it here.
set -euo pipefail

MODE="${1:?usage: 08-gatehouse-repo.sh create|verify <repo> [resident...]}"
REPO_NAME="${2:?usage: 08-gatehouse-repo.sh create|verify <repo> [resident...]}"
shift 2
RESIDENTS=("$@")

GATEHOUSE_DIR="${GATEHOUSE_DIR:-/var/lib/disjorn-broker/gatehouse}"
GATEHOUSE_GROUP="${GATEHOUSE_GROUP:-gatehouse}"
BROKER_USER="${BROKER_USER:-plink}"
REPO="$GATEHOUSE_DIR/$REPO_NAME.git"

say()  { echo "08-gatehouse-repo: $*"; }
warn() { echo "08-gatehouse-repo: $*" >&2; }
die()  { echo "08-gatehouse-repo: $*" >&2; exit 1; }

# The repo name becomes a path segment and a clone target; the resident names
# become account names. Both are validated here rather than trusted, on the same
# reasoning disjorn-build-launch gives for re-validating its slug.
case "$REPO_NAME" in
  ""|-*|*.git|*[!a-zA-Z0-9._-]*)
     die "repo name must be a plain name (no .git suffix, no path, no leading -): $REPO_NAME" ;;
esac
for _r in ${RESIDENTS+"${RESIDENTS[@]}"}; do
  case "$_r" in
    ""|*[!a-z0-9]*) die "resident must be a plain lowercase name, no res- prefix: $_r" ;;
  esac
done
unset _r

# ── create ───────────────────────────────────────────────────────────────
do_create() {
  [ "$(id -u)" -eq 0 ] || die "create needs root (group creation, chown, setgid, /etc/gitconfig) — run it with sudo"
  [ -d "$GATEHOUSE_DIR" ] || die "gatehouse directory missing: $GATEHOUSE_DIR"
  id "$BROKER_USER" >/dev/null 2>&1 || die "broker user does not exist: $BROKER_USER"

  # The group first: everything below names it.
  if getent group "$GATEHOUSE_GROUP" >/dev/null; then
    say "group $GATEHOUSE_GROUP exists"
  else
    groupadd --system "$GATEHOUSE_GROUP"
    say "created group $GATEHOUSE_GROUP"
  fi

  # Membership. The broker user is in it because it owns the repos; each named
  # resident is in it because it pushes into them. A grant is printed, never
  # silent.
  local members=("$BROKER_USER") r
  for r in ${RESIDENTS+"${RESIDENTS[@]}"}; do members+=("res-$r"); done
  local u
  for u in "${members[@]}"; do
    id "$u" >/dev/null 2>&1 || die "account does not exist: $u (harness/keyboard/01-users.sh)"
    if id -nG "$u" | tr ' ' '\n' | grep -qx "$GATEHOUSE_GROUP"; then
      say "$u already in $GATEHOUSE_GROUP"
    else
      usermod -aG "$GATEHOUSE_GROUP" "$u"
      say "GRANTED: added $u to group $GATEHOUSE_GROUP"
      # res-* accounts have no login shell and their processes are started by
      # systemd (systemd-run --uid, or the user unit), which builds the group
      # list fresh at start — so nothing needs to re-login. A shell plink
      # already has open will NOT see the new group, which is one more reason
      # verify probes with `sudo -u` instead of asking the current shell.
    fi
  done

  if [ -d "$REPO" ]; then
    say "$REPO exists — re-applying the recipe (refs, objects and hooks untouched)"
  else
    # --shared=group makes git apply its half from the first byte, so even the
    # directories `git init` creates for itself are right before anything else
    # runs. This is the "at creation" in "not repaired after".
    git init --bare --shared=group "$REPO" >/dev/null
    say "created bare repo $REPO"
  fi

  chown -R "$BROKER_USER:$GATEHOUSE_GROUP" "$REPO"
  chmod -R g+rwX "$REPO"
  # setgid on EVERY directory, including the repo root and everything git has
  # already made under it. This is the property that governs files that do not
  # exist yet.
  find "$REPO" -type d -exec chmod g+s {} +
  git -C "$REPO" config core.sharedRepository group

  # The ownership exemption, per repo, in the system config. Idempotent: git
  # would happily add a duplicate line.
  if git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$REPO"; then
    say "safe.directory already names $REPO in /etc/gitconfig"
  else
    git config --system --add safe.directory "$REPO"
    say "added safe.directory=$REPO to /etc/gitconfig (per-repo, never '*')"
  fi

  say "recipe applied: owner=$BROKER_USER group=$GATEHOUSE_GROUP setgid+g+rwX core.sharedRepository=group safe.directory"
}

# ── verify ───────────────────────────────────────────────────────────────
# Everything here is checked by LOOKING, never by trusting the create path —
# including when it runs three lines after it. A recipe that reports itself
# applied is the failure mode this file exists to prevent.
VERIFY_BAD=0
bad()  { echo "08-gatehouse-repo: FAIL: $*" >&2; VERIFY_BAD=1; }
ok()   { echo "08-gatehouse-repo: ok: $*"; }
list() { while IFS= read -r _l; do [ -n "$_l" ] && echo "    $_l" >&2; done; }

do_verify() {
  [ -d "$REPO" ] || die "no such gatehouse repo: $REPO"

  local owner group
  owner="$(stat -c %U "$REPO")"
  group="$(stat -c %G "$REPO")"
  if [ "$owner" = "$BROKER_USER" ]; then ok "owner is $BROKER_USER"
  else bad "owner is $owner, expected the broker user $BROKER_USER"; fi
  if [ "$group" = "$GATEHOUSE_GROUP" ]; then ok "group is $GATEHOUSE_GROUP"
  else bad "group is $group, expected $GATEHOUSE_GROUP"; fi

  # THE WRONG-GROUP FILE CHECK (spec architecture note 5). One find, over
  # everything, because the whole hazard is that ONE file in ONE directory
  # carries the creating process's private group and nothing notices until the
  # other uid touches exactly that file. Run it before the first push, and
  # again after: a repo that keeps coming back empty is a repo whose setgid
  # bits are doing their job.
  local wrong
  wrong="$(find "$REPO" ! -group "$GATEHOUSE_GROUP" -printf '%p (group %g)\n' 2>/dev/null || true)"
  if [ -n "$wrong" ]; then
    bad "WRONG-GROUP FILES — setgid is not holding:"
    printf '%s\n' "$wrong" | list
  else
    ok "wrong-group file check: every path is group $GATEHOUSE_GROUP"
  fi

  local nosgid
  nosgid="$(find "$REPO" -type d ! -perm -2000 -print 2>/dev/null || true)"
  if [ -n "$nosgid" ]; then
    bad "directories WITHOUT setgid — files created in them inherit the wrong group:"
    printf '%s\n' "$nosgid" | list
  else
    ok "every directory is setgid"
  fi

  local nogw
  nogw="$(find "$REPO" ! -perm -020 -print 2>/dev/null || true)"
  if [ -n "$nogw" ]; then
    bad "paths that are NOT group-writable:"
    printf '%s\n' "$nogw" | list
  else
    ok "every path is group-writable"
  fi

  local shared
  shared="$(git -C "$REPO" config --get core.sharedRepository || true)"
  case "$shared" in
    group|1) ok "core.sharedRepository=$shared" ;;
    "")      bad "core.sharedRepository is UNSET — git will write 0644 objects and the next uid loses" ;;
    *)       bad "core.sharedRepository=$shared, expected group" ;;
  esac

  if git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$REPO"; then
    ok "safe.directory names this repo in /etc/gitconfig"
  else
    bad "safe.directory does NOT name $REPO — git will refuse this repo from any uid that does not own it (\"detected dubious ownership\"), which is every resident"
  fi

  # ── the resident-seat probe ───────────────────────────────────────────
  # BUILD-A LESSON, 2026-08-07: the group layer gets verified FROM A RESIDENT
  # SEAT, never keyboard-reported. Every check above answers "do the bits look
  # right to root", and root is the one uid for which that answer is always
  # yes. The question that matters is whether res-<name> can write an object
  # the broker can then read — so ask res-<name>.
  #
  # `git hash-object -w` is the smallest real exercise of the whole path: git
  # creates the fan-out directory (setgid decides its group), writes a loose
  # object (core.sharedRepository decides its mode), and both are then checked
  # from outside. The object is dangling and is removed again; nothing else in
  # the repo is touched, and no ref moves.
  local r acct sha objpath objgroup objmode
  for r in ${RESIDENTS+"${RESIDENTS[@]}"}; do
    acct="res-$r"
    # Root first, and deliberately before the account check: "can a seat probe
    # be run at all" is prior to "does this seat exist", and answering in that
    # order means an unprivileged run never reports a group layer it did not
    # actually test.
    if [ "$(id -u)" -ne 0 ]; then
      bad "NOT ROOT, so the $acct seat probe did not run. A keyboard-reported group layer is exactly what the 08-07 lesson says not to accept — re-run with sudo before the first push."
      continue
    fi
    if ! id "$acct" >/dev/null 2>&1; then
      bad "$acct does not exist — cannot verify the group layer from its seat"
      continue
    fi
    if ! id -nG "$acct" | tr ' ' '\n' | grep -qx "$GATEHOUSE_GROUP"; then
      bad "$acct is NOT in group $GATEHOUSE_GROUP"
      continue
    fi
    if ! sha="$(printf 'gatehouse seat probe\n' | sudo -u "$acct" git -C "$REPO" hash-object -w --stdin 2>&1)"; then
      bad "$acct CANNOT write an object into $REPO — this is the real answer, and it is no: $sha"
      continue
    fi
    objpath="$REPO/objects/${sha:0:2}/${sha:2}"
    if [ ! -f "$objpath" ]; then
      bad "$acct reported writing object $sha but it is not at $objpath"
      continue
    fi
    objgroup="$(stat -c %G "$objpath")"
    objmode="$(stat -c %a "$objpath")"
    if [ "$objgroup" != "$GATEHOUSE_GROUP" ]; then
      bad "the object $acct wrote landed in group $objgroup, not $GATEHOUSE_GROUP — setgid is not holding for NEW files, the failure that only ever shows up on someone else's fetch"
    elif [ "$(stat -c %A "$objpath" | cut -c6)" != "w" ]; then
      bad "the object $acct wrote is not group-writable (mode $objmode) — core.sharedRepository is not being honoured"
    else
      ok "$acct wrote a real object: group=$objgroup mode=$objmode (verified from the seat, not reported)"
    fi
    rm -f "$objpath"
    rmdir "$REPO/objects/${sha:0:2}" 2>/dev/null || true
  done

  if [ "${#RESIDENTS[@]}" -eq 0 ]; then
    warn "NOTE no residents named, so the group layer was NOT verified from any seat. Pass the residents that push to this repo."
  fi

  [ "$VERIFY_BAD" -eq 0 ] || die "verification FAILED — do not push into this repo until it passes"
  say "VERIFIED: $REPO"
}

case "$MODE" in
  create) do_create; echo; do_verify ;;
  verify) do_verify ;;
  *) die "unknown mode: $MODE (expected create or verify)" ;;
esac
