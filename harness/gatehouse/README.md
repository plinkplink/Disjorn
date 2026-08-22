# harness/gatehouse — the canonical repo's perimeter

Things that live ON `disjorn.git` itself rather than in a lane. Today that is
one hook.

## `hooks/pre-receive-main-review` — the keyboard-lane gate (Plan Room Phase 0)

Refuses a `main` ref update whose commit range touches `server/`, `client/`,
`sdk/` or `harness/` unless the pushed head's commit message carries a
`review-seq: <n>` or `override-seq: <n>` trailer. Presence check only — the
hook is deliberately dumb; resolving the seq is the daily digest's job
(`harness/metrics/metrics.py`, the GATE DRIFT block). Everything else passes
untouched: non-main branches, gatehouse refs, doc-only ranges.

**It fails open by design.** Any error other than a definite missing trailer
allows the push and warns loudly. A gate that can wedge the keyboard while the
reviewers are down is worse than the disease it treats. The digest counts every
fail-open firing.

**The override convention.** Post in #custodian:

```
keyboard: override-merge <slug> — <reason>
```

then put that seq in the trailer as `override-seq: <n>`. Review is still owed
within a day. An override is an ordinary legible act, not a violation — the
count is the control, and it is derived from `main`'s trailers at digest time,
so it survives any database rebuild.

### Install — two plink acts, in this order

The exact host path of the canonical repo is known at the keyboard; on this
deployment it is `/var/lib/disjorn-broker/gatehouse/disjorn.git`.

1. **Deploy a copy of the hook under the broker's tree, and symlink to THAT.**
   Never symlink into a working clone (seq 1428, G4): a `git checkout` in a
   clone would silently disarm the gate, and nothing would say so.

   ```sh
   sudo install -o disjorn-broker -g disjorn-broker -m 0755 \
        harness/gatehouse/hooks/pre-receive-main-review \
        /var/lib/disjorn-broker/gatehouse/hooks-deployed/pre-receive-main-review
   sudo ln -sfn /var/lib/disjorn-broker/gatehouse/hooks-deployed/pre-receive-main-review \
        /var/lib/disjorn-broker/gatehouse/disjorn.git/hooks/pre-receive
   ```

2. **Seed the push log's floor**, so it predates the first push:

   ```sh
   sudo -u disjorn-broker \
        /var/lib/disjorn-broker/gatehouse/hooks-deployed/pre-receive-main-review \
        --seed-genesis \
        --git-dir /var/lib/disjorn-broker/gatehouse/disjorn.git
   ```

   Re-running step 2 is a no-op. If you skip it the hook seeds the floor
   *lazily* at its first firing instead — the floor's existence never depends
   on a hand step being remembered — but a lazy floor is a warning state, not a
   healthy default: it is minted from whatever `main` looked like the first
   time the hook happened to fire, so the digest reports everything below it as
   **unverifiable**, never clean.

Then confirm from the digest, not from the shell: the GATE DRIFT block's first
line names the installed hook path, the sha of the file the symlink resolves
to, and the mirror's sha for `harness/gatehouse/hooks/pre-receive-main-review`.
Committed is not installed — that went four-for-four on 08-19/20.

### The push log

One appended line per `main` push decision, at
`<git-dir>/hooks/disjorn-push-log` (override with `DISJORN_PUSH_LOG`; if you
do, set `[gate].push_log` in `broker.toml` to match or the digest goes blind —
loudly, on its liveness line).

```
GENESIS seeded 2026-08-20T11:00:00Z <main head at install>
GENESIS lazy   2026-08-20T11:00:00Z <old sha of the triggering push>
PUSH 2026-08-20T11:03:11Z <old>..<new> review-seq:1428 passed
PUSH 2026-08-20T11:09:02Z <old>..<new> NONE refused
PUSH 2026-08-20T11:12:40Z <old>..<new> NONE failed-open
```

It is **append-only and not rebuildable**. Push boundaries and fail-open
firings exist nowhere in git and cannot be derived after the fact, so this is a
primary record — the same class as the broker audit log, not a cache. Two
things in the digest have no other source: the fail-open count, and *uncovered*
commits (anything that entered `main` above the floor with no covering log
line, which means it arrived while the hook was absent or disarmed).

Do not edit it, rotate it, or recreate it. The digest arms three tamper tells:
a second genesis line means the log was deleted and recreated; a first line
that is not a genesis line means it was truncated; and a floor that has moved
since the previous digest post means the log was replaced whatever the log
itself claims — that last one is checked against the message store, outside the
git-dir, so it survives losing the log entirely.

### Relationship to BUILD-LANE-V2 stage 2b

Stage 2b names this same hook as part of the canonical-repo perimeter (seqs
1209/1212 — chown to the broker user, drop resident group-write, pre-receive
hook). This is the hook 2b installs; 2b installs rather than reinvents.
Nothing here blocks or presumes the rest of 2b.

### Tests

```sh
python3 -m pytest harness/gatehouse/tests -q
```

They drive a real bare repo through real `git push`es — the hook's own report
is the thing under test, so the assertions read the refs and the log.
