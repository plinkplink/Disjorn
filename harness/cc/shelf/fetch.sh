#!/usr/bin/env bash
# fetch.sh — (re)vendor the apps-builder asset shelf from MANIFEST.toml.
#
# RUN BY: a human, in the repo, when MANIFEST.toml changes. NOT by the image
# build and NOT by the seat: the vendored files are committed, and
# Containerfile.apps only COPYs them. Nothing at turn time ever downloads.
#
#     bash harness/cc/shelf/fetch.sh            # download, verify, extract
#     bash harness/cc/shelf/fetch.sh --verify   # re-hash the shelf, no network
#     SHELF_CACHE=/path/to/tgz bash .../fetch.sh   # use local tarballs
#
# Idempotent: a second run rewrites identical bytes. Exits non-zero the moment
# any digest disagrees with MANIFEST.toml — a mismatch is never repaired,
# never overwritten, only reported.
#
# Deps: bash, awk, curl, tar, sha256sum, mktemp. No node, no python, no jq.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
manifest="$here/MANIFEST.toml"
mode="fetch"
case "${1-}" in
  "")        ;;
  --verify)  mode="verify" ;;
  *) echo "usage: $(basename "$0") [--verify]" >&2; exit 64 ;;
esac

[[ -r "$manifest" ]] || { echo "fetch.sh: no MANIFEST.toml at $manifest" >&2; exit 1; }

fail=0
note() { printf '%s\n' "$*"; }
bad()  { printf 'FAIL: %s\n' "$*" >&2; fail=1; }

# MANIFEST.toml -> one tab-separated record per [[file]] block, fields in a
# fixed order. Any missing field is a manifest bug and stops the run.
readonly FIELDS="slot package version tarball_url tarball_sha256 path_in_tarball shelf_path sha256 license license_in_tarball license_shelf_path"
records="$(
  awk -v fields="$FIELDS" '
    function flush(   i, n, k, out) {
      if (!have) return
      n = split(fields, F, " ")
      out = ""
      for (i = 1; i <= n; i++) {
        k = F[i]
        if (!(k in v)) { printf("MANIFEST %s: entry ending line %d has no %s\n", FILENAME, NR, k) > "/dev/stderr"; exit 1 }
        out = out (i == 1 ? "" : "\t") v[k]
      }
      print out
      delete v; have = 0
    }
    /^[[:space:]]*\[\[file\]\][[:space:]]*$/ { flush(); have = 1; next }
    /^[[:space:]]*#/ { next }
    have && /^[[:space:]]*[a-z0-9_]+[[:space:]]*=[[:space:]]*".*"[[:space:]]*$/ {
      key = $0; sub(/^[[:space:]]*/, "", key); sub(/[[:space:]]*=.*$/, "", key)
      val = $0; sub(/^[^"]*"/, "", val); sub(/"[[:space:]]*$/, "", val)
      v[key] = val; next
    }
    END { flush() }
  ' "$manifest"
)"
[[ -n "$records" ]] || { echo "fetch.sh: MANIFEST.toml has no [[file]] entries" >&2; exit 1; }

# sha256 of $1, or empty if it does not exist.
digest() { [[ -f "$1" ]] && sha256sum "$1" | cut -d' ' -f1 || true; }

check() { # check <path> <want> <label>
  local got; got="$(digest "$1")"
  if [[ -z "$got" ]]; then bad "$3: missing ($1)"; return 1; fi
  if [[ "$got" != "$2" ]]; then bad "$3: sha256 $got, MANIFEST says $2"; return 1; fi
  note "  ok  $3"
}

if [[ "$mode" == verify ]]; then
  note "verifying $here against MANIFEST.toml"
  while IFS=$'\t' read -r slot pkg ver url tsha member shelf sha lic licsrc licdst; do
    check "$here/$shelf" "$sha" "$shelf" || true
    [[ -s "$here/$licdst" ]] || bad "$shelf: licence $licdst missing or empty"
  done <<< "$records"
  (( fail == 0 )) || { echo "shelf VERIFY FAILED" >&2; exit 1; }
  note "shelf verify OK"
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# One download per tarball, however many files come out of it.
tarball_for() { # tarball_for <url> -> path in $work, downloaded+verified once
  local url="$1" want="$2" name tgz
  name="${url##*/}"
  tgz="$work/$name"
  if [[ ! -f "$tgz" ]]; then
    if [[ -n "${SHELF_CACHE-}" && -f "$SHELF_CACHE/$name" ]]; then
      cp -- "$SHELF_CACHE/$name" "$tgz"
      note "  cache $name" >&2
    else
      note "  get   $url" >&2
      curl -fsSL --retry 3 --max-time 300 -o "$tgz" -- "$url"
    fi
    local got; got="$(digest "$tgz")"
    if [[ "$got" != "$want" ]]; then
      echo "FAIL: $name: tarball sha256 $got, MANIFEST says $want" >&2
      exit 1
    fi
  fi
  printf '%s' "$tgz"
}

note "vendoring into $here"
while IFS=$'\t' read -r slot pkg ver url tsha member shelf sha lic licsrc licdst; do
  tgz="$(tarball_for "$url" "$tsha")"
  ex="$work/x"; rm -rf "$ex"; mkdir -p "$ex"
  # Extract ONLY the two named members. No wildcards, no whole-tree unpack.
  tar -xzf "$tgz" -C "$ex" --no-same-owner -- "$member" "$licsrc"
  mkdir -p "$here/$(dirname "$shelf")"
  cp -- "$ex/$member" "$here/$shelf"
  cp -- "$ex/$licsrc" "$here/$licdst"
  chmod 0644 "$here/$shelf" "$here/$licdst"
  check "$here/$shelf" "$sha" "$shelf ($pkg@$ver, $lic)" || true
done <<< "$records"

(( fail == 0 )) || { echo "shelf FETCH FAILED — nothing above may be committed" >&2; exit 1; }
note "shelf fetch OK — now: git add, and re-run --verify from a clean tree"
