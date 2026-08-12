#!/usr/bin/env bash
# 09-build-lane-preflight.sh — read-only pre-flight for a resident's build lane.
#
# RUN BY: plink, with sudo (the seat probes need it), BEFORE the first build in
# a lane and again whenever anything in the lane is deployed:
#   sudo bash 09-build-lane-preflight.sh gable
#
# CHANGES NOTHING. Every check reads, stats, diffs or probes. The one thing it
# writes is a dangling git object it then deletes, inside the seat probe it
# delegates to 08-gatehouse-repo.sh, and that is the point of the probe.
#
# WHY: SPECS/2026-08-08-gable-build-lane-provisioning.md, architecture note 5.
# Two named checks, plus the two Build-A lessons that share their shape:
#
#   1. DIFF THE DEPLOYED CONFIG AGAINST THE REPO COPY. The stale-deploy family
#      is this project's most reliable way to lose an evening. It has fired at
#      least four times: an uninstalled fix, a stale container serving old code,
#      `run-build.sh` never deployed at all while `[start_build].command`
#      already pointed at it, and a resident reported a missing tool that was
#      genuinely missing because her container was a day behind the image. Every
#      one of them looked like a code bug and was a deploy fact. So: diff first,
#      then believe what you read.
#
#   2. THE WRONG-GROUP FILE CHECK on the lane's gatehouse repo, before its first
#      push — delegated to 08-gatehouse-repo.sh verify, including its
#      resident-seat probe. Verified FROM THE SEAT, never keyboard-reported
#      (2026-08-07).
#
#   3. GATEHOUSE main VS CANONICAL main. The stale-base hazard, which fired live
#      2026-08-07: a build clones from the gatehouse, so gatehouse `main` is the
#      base every build starts from. Merge into the canonical repo and forget to
#      push main BACK to the gatehouse, and the next build branches off
#      yesterday's tree — a conflict at merge time if you are lucky, and a silent
#      revert of the last merge if you are not. Push main back after EVERY merge;
#      this check is how you find out you didn't.
#
#   4. THE LAUNCH-BLOCKING GROUND for the seat: the spine mirror (run-build.sh
#      refuses to launch if RESIDENT_SPINE_HOST names a missing directory, or one
#      writable by the uid it runs as) and the credential (the build seat is
#      Max-only and refuses to launch on an API key alone). Both are new refusal
#      paths as of 2026-08-12; a pre-flight that finds them beats a build that
#      dies at second three.
#
# Env overrides (staging/tests; production uses the defaults):
#   REPO_ROOT          default /home/plink/Disjorn/Disjorn
#   GATEHOUSE_DIR      default /var/lib/disjorn-broker/gatehouse
#   GATEHOUSE_REPO     default <resident>  (the lane's own repo: <name>.git)
#   SPINE_MIRROR_ROOT  default /srv/disjorn-spine
#   BUILD_CONFIG_ROOT  default /srv/disjorn-build-config
#   BROKER_ETC         default /etc/disjorn-broker
#   DISJORN_LIB        default /usr/local/lib/disjorn
set -euo pipefail

NAME="${1:?usage: 09-build-lane-preflight.sh <resident>   (e.g. gable)}"
case "$NAME" in
  ""|*[!a-z0-9]*) echo "09-preflight: resident must be a plain lowercase name, no res- prefix: $NAME" >&2; exit 1 ;;
esac

REPO_ROOT="${REPO_ROOT:-/home/plink/Disjorn/Disjorn}"
GATEHOUSE_DIR="${GATEHOUSE_DIR:-/var/lib/disjorn-broker/gatehouse}"
GATEHOUSE_REPO="${GATEHOUSE_REPO:-$NAME}"
SPINE_MIRROR_ROOT="${SPINE_MIRROR_ROOT:-/srv/disjorn-spine}"
BUILD_CONFIG_ROOT="${BUILD_CONFIG_ROOT:-/srv/disjorn-build-config}"
BROKER_ETC="${BROKER_ETC:-/etc/disjorn-broker}"
DISJORN_LIB="${DISJORN_LIB:-/usr/local/lib/disjorn}"
ACCT="res-$NAME"

BAD=0
NOTES=0
sec()  { echo; echo "── $* ─────────────────────────────────────────────"; }
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*" >&2; BAD=1; }
note() { echo "  NOTE  $*" >&2; NOTES=$((NOTES + 1)); }

echo "09-build-lane-preflight: lane=$NAME  repo=$REPO_ROOT  (read-only)"
[ -d "$REPO_ROOT" ] || { echo "09-preflight: repo root missing: $REPO_ROOT" >&2; exit 1; }

# ── 1. stale deploy ──────────────────────────────────────────────────────
# CODE artifacts must be byte-identical to the repo copy: they are deployed by
# `install`, so any difference means someone edited the deployed copy or forgot
# to re-install. CONFIG artifacts are STATE — broker.toml and verbs.toml are
# meant to differ from their templates (uids, the kill switches, the model pin),
# so the diff is printed for reading and never counted as a failure. Telling
# those two categories apart is the whole reason this is a script and not
# `diff -r`.
sec "1. deployed vs repo (stale-deploy family)"

diff_code() {  # diff_code <deployed> <repo-relative>
  local dep="$1" src="$REPO_ROOT/$2"
  if [ ! -e "$src" ]; then bad "repo copy missing: $src"; return; fi
  if [ ! -e "$dep" ]; then
    bad "NOT DEPLOYED: $dep (repo has $2) — the config that names it will fail closed at launch"
    return
  fi
  if diff -r -q "$dep" "$src" >/dev/null 2>&1; then
    ok "identical: $dep"
  else
    bad "STALE DEPLOY: $dep differs from $2"
    diff -r -u "$dep" "$src" 2>&1 | sed 's/^/        /' >&2 || true
  fi
}

diff_config() {  # diff_config <deployed> <repo-relative> — report only
  local dep="$1" src="$REPO_ROOT/$2"
  if [ ! -e "$dep" ]; then bad "NOT INSTALLED: $dep (template $2)"; return; fi
  if diff -q "$dep" "$src" >/dev/null 2>&1; then
    ok "identical to template: $dep"
  else
    note "$dep differs from the template $2 — EXPECTED (it is state, not code). Read the diff and confirm every line is a decision someone made:"
    diff -u "$src" "$dep" 2>&1 | sed 's/^/        /' >&2 || true
  fi
}

diff_code "$DISJORN_LIB/disjorn-build-launch" harness/broker/disjorn-build-launch
diff_code "$DISJORN_LIB/run-build.sh"         harness/cc/run-build.sh
diff_code "$DISJORN_LIB/run-resident.sh"      harness/cc/run-resident.sh
diff_code "$DISJORN_LIB/build-kernel.md"      harness/cc/build-kernel.md
diff_code "$DISJORN_LIB/house_memory"         harness/house_memory/house_memory
diff_code /etc/sudoers.d/91-disjorn-build     harness/keyboard/91-disjorn-build.sudoers
diff_config "$BROKER_ETC/broker.toml"         harness/broker/broker.toml
diff_config "$BROKER_ETC/verbs.toml"          harness/broker/verbs.toml

# The helper's own two invariants, checked where they are true or false: on disk.
if [ -e "$DISJORN_LIB/disjorn-build-launch" ]; then
  _o="$(stat -c %U "$DISJORN_LIB/disjorn-build-launch")"
  _m="$(stat -c %a "$DISJORN_LIB/disjorn-build-launch")"
  [ "$_o" = root ] && ok "launch helper is root-owned" \
    || bad "launch helper is owned by $_o, not root — it refuses to run itself in this state, and rightly"
  case "$_m" in *[2367]) bad "launch helper is group- or world-writable (mode $_m)" ;; *) ok "launch helper mode $_m" ;; esac
fi

# ── 2. the gatehouse repo ────────────────────────────────────────────────
sec "2. gatehouse repo + the wrong-group file check (from the seat)"
_gh_script="$(dirname "$0")/08-gatehouse-repo.sh"
if [ ! -f "$_gh_script" ]; then
  bad "08-gatehouse-repo.sh not found next to this script ($_gh_script) — cannot run the group verification"
elif GATEHOUSE_DIR="$GATEHOUSE_DIR" bash "$_gh_script" verify "$GATEHOUSE_REPO" "$NAME" 2>&1 | sed 's/^/  /'; then
  ok "gatehouse repo $GATEHOUSE_REPO.git verified (recipe + wrong-group + seat probe)"
else
  bad "gatehouse repo $GATEHOUSE_REPO.git FAILED verification — do not push into it (output above)"
fi

# ── 3. stale base ────────────────────────────────────────────────────────
sec "3. gatehouse main vs canonical main (stale-base hazard, fired live 08-07)"
for _repo_path in "$GATEHOUSE_DIR"/*.git; do
  [ -d "$_repo_path" ] || continue
  _repo="$(basename "$_repo_path" .git)"
  _gh_main="$(git -C "$_repo_path" rev-parse --verify -q main 2>/dev/null || true)"
  if [ -z "$_gh_main" ]; then
    note "$_repo.git has no main branch — a build cloning it gets no base. Push main into it once."
    continue
  fi
  # The canonical counterpart. Only the Disjorn repo's canonical path is known
  # here; anything else is reported rather than guessed at, because guessing a
  # path and diffing against the wrong tree is worse than saying "unknown".
  _canon=""
  case "$_repo" in
    disjorn) _canon="$REPO_ROOT" ;;
  esac
  if [ -z "$_canon" ] || [ ! -d "$_canon/.git" ]; then
    note "$_repo.git: gatehouse main is $(echo "$_gh_main" | cut -c1-12); no canonical path known to this script — compare it by hand"
    continue
  fi
  _canon_main="$(git -C "$_canon" rev-parse --verify -q main 2>/dev/null || true)"
  if [ "$_gh_main" = "$_canon_main" ]; then
    ok "$_repo.git main == canonical main ($(echo "$_gh_main" | cut -c1-12))"
  else
    bad "$_repo.git main is $(echo "$_gh_main" | cut -c1-12) but canonical main is $(echo "$_canon_main" | cut -c1-12) — the next build would branch off a STALE BASE. Push main back to the gatehouse (that is the after-every-merge step)."
  fi
done
unset _repo_path _repo _gh_main _canon _canon_main

# ── 4. the launch-blocking ground for this seat ──────────────────────────
sec "4. the seat's ground: spine mirror, build config, credential route"

_spine="$SPINE_MIRROR_ROOT/$NAME"
if [ ! -d "$_spine" ]; then
  note "no spine mirror at $_spine — disjorn-build-launch will set no RESIDENT_SPINE_HOST and the build gets no /opt/spine (unchanged behaviour). Publish one with 06-spine-mirror.sh $NAME if this seat should have it."
else
  ok "spine mirror present: $_spine"
  # UNWRITABLE FROM THE SEAT, asked of the seat. run-build.sh runs this same
  # test as the res-* uid and REFUSES TO LAUNCH if it passes, so a mirror that
  # looks fine from root and is writable from res-* is a build that dies at
  # launch — and, worse, a kernel the resident could rewrite.
  if [ "$(id -u)" -ne 0 ]; then
    bad "NOT ROOT: cannot ask $ACCT whether $_spine is writable, and root's own answer is worth nothing here. Re-run with sudo."
  elif ! id "$ACCT" >/dev/null 2>&1; then
    bad "$ACCT does not exist — cannot verify the spine wall from its seat"
  elif sudo -u "$ACCT" find "$_spine" -maxdepth 1 -writable -print -quit 2>/dev/null | grep -q .; then
    bad "$_spine is WRITABLE by $ACCT — that is the kernel, and run-build.sh will refuse to launch. Do NOT loosen the canonical spine to fix it; re-publish the mirror (06-spine-mirror.sh)."
  else
    ok "$_spine is not writable by $ACCT (asked $ACCT, not reported)"
  fi
fi

_cfg="$BUILD_CONFIG_ROOT/$NAME"
if [ ! -d "$_cfg" ]; then
  bad "build config dir missing: $_cfg — run-build.sh exits before podman without it"
else
  ok "build config dir present: $_cfg"
  if [ ! -f "$_cfg/settings.json" ]; then
    note "$_cfg/settings.json absent — the build seat has no permission set of its own (template: harness/cc/build-config/settings.json)"
  else
    ok "$_cfg/settings.json present"
  fi
  # THE CREDENTIAL ROUTE. Names only — this script never prints a value, and
  # never needs to. The build seat is Max-only: OAuth present is a pass, API key
  # alone is a launch refusal, neither is a session that cannot authenticate.
  if [ ! -f "$_cfg/env" ]; then
    bad "$_cfg/env absent — the build session will have no credential at all"
  else
    _has_oauth=no; _has_key=no
    grep -qE '^[[:space:]]*CLAUDE_CODE_OAUTH_TOKEN=.' "$_cfg/env" && _has_oauth=yes
    grep -qE '^[[:space:]]*ANTHROPIC_API_KEY=.' "$_cfg/env" && _has_key=yes
    if [ "$_has_oauth" = yes ] && [ "$_has_key" = yes ]; then
      note "$_cfg/env holds BOTH credentials. OAuth wins and the API key is filtered out, so this is safe — but a metered key sitting in a Max-only seat's env file is a route waiting to be taken by accident. Remove it."
    elif [ "$_has_oauth" = yes ]; then
      ok "$_cfg/env routes to the Max account (CLAUDE_CODE_OAUTH_TOKEN present)"
    elif [ "$_has_key" = yes ]; then
      bad "$_cfg/env holds ONLY ANTHROPIC_API_KEY. The build seat is Max-only and run-build.sh will REFUSE TO LAUNCH rather than silently spend the metered key (SPECS/2026-08-05-credential-routing-and-halt-protocol.md §3). Mint a token: claude setup-token."
    else
      bad "$_cfg/env holds neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY — the session will fail to authenticate"
    fi
    _mode="$(stat -c %a "$_cfg/env")"
    case "$_mode" in 600|640|400|440) ok "$_cfg/env mode $_mode" ;;
                     *) bad "$_cfg/env is mode $_mode — a credential file should not be group- or world-readable" ;; esac
  fi
fi

if [ -d "/home/$ACCT" ]; then
  if [ -d "/home/$ACCT/build-home" ]; then
    ok "build home present: /home/$ACCT/build-home"
  else
    bad "build home missing: /home/$ACCT/build-home — run-build.sh exits before podman without it"
  fi
else
  note "/home/$ACCT not present or not readable from here"
fi

# ── 5. the verb, and the identity it runs under ──────────────────────────
sec "5. the verb surface"
if [ -f "$BROKER_ETC/verbs.toml" ]; then
  # Which residents have start-build ON. BR-1 (DEFERRED.md): the build identity
  # comes from the GLOBAL [start_build].resident, not from the caller's
  # SO_PEERCRED uid, so with two residents holding the verb the audit records
  # the configured name and not the actor — "wrong half the time", in the words
  # of the item. This does not fail the pre-flight; it is plink's ruling to make,
  # and it must be made with the fact in front of him rather than after.
  _on="$(awk '/^\[/{sect=$0} /"start-build"[[:space:]]*=[[:space:]]*true/{print sect}' "$BROKER_ETC/verbs.toml" | tr -d '[]' | tr '\n' ' ')"
  _cfg_resident="$(awk -F'"' '/^resident[[:space:]]*=/{print $2}' "$BROKER_ETC/broker.toml" 2>/dev/null | tail -n1)"
  ok "start-build is ON for: ${_on:-(nobody)}"
  ok "[start_build].resident = ${_cfg_resident:-(unset)}"
  _count=$(echo "$_on" | wc -w)
  if [ "$_count" -gt 1 ]; then
    note "TWO OR MORE residents hold start-build while the build identity is one global config value ($_cfg_resident). That is BR-1, and DEFERRED.md calls it 'required before a second resident gets the verb': a build started by one resident runs, and audits, as the other. Nothing in this lane fixes it — the fix is deriving the identity from the caller's SO_PEERCRED uid in brokerd.py."
  fi
  if [ -n "$_cfg_resident" ] && ! echo " $_on " | grep -q " res-$_cfg_resident "; then
    note "[start_build].resident is $_cfg_resident but res-$_cfg_resident does not have start-build ON. Every build launched by anyone will still run as $_cfg_resident."
  fi
else
  note "$BROKER_ETC/verbs.toml not readable from here — cannot report the verb state"
fi
# The build container's verb surface is decided by a MOUNT, not a toggle: there
# is no broker socket in a build container at all. Checked against the deployed
# wrapper, which is the copy that runs.
if [ -f "$DISJORN_LIB/run-build.sh" ]; then
  if grep -q 'run-disjorn-broker\|/run/disjorn-broker' "$DISJORN_LIB/run-build.sh" \
     && ! grep -q 'NO BROKER SOCKET' "$DISJORN_LIB/run-build.sh"; then
    bad "the DEPLOYED run-build.sh mounts the broker socket — a build could call start-build from inside a build"
  else
    ok "deployed run-build.sh mounts no broker socket (propose/read only; no verbs at all)"
  fi
fi

# ── verdict ──────────────────────────────────────────────────────────────
echo
if [ "$BAD" -ne 0 ]; then
  echo "09-build-lane-preflight: NOT READY — $NOTES note(s) and at least one FAIL above. Fix the FAILs before the first build; do not push into a repo that failed §2." >&2
  exit 1
fi
echo "09-build-lane-preflight: READY for lane $NAME ($NOTES note(s) above — read them, they are not failures but they are not nothing either)."
