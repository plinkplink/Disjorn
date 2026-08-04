# House error log

The artifact Memory v2 left open as *"house error log owner + location — plink
07-28: separate initiative, backlogged."* **Ruled 2026-08-03**: owner is plink
+ the keyboard Claude Code harness, location is `/var/log/disjorn-errorlog/errors.jsonl`.

## Why it exists

On 2026-07-28 at 14:47 UTC Claudette wrote a full reply to plink, hit the token
wall, and what reached the channel was `No response generated.` Her log said
`stop_reason=max_tokens`. Nobody noticed until she went looking, and the reply
was gone. Gable has the same signature — typing for a while, then dropping.

**A turn that dies should leave a record somewhere a human reads.** That is the
whole job.

## Why plink owns it and the residents don't

The original plan was resident-owned harnesses filing their own faults. That
does not survive contact with the walls we built on purpose: residents are
sealed in containers with no write path to a house-level file, and changing
anything inside one is a multi-step deploy. A resident-owned error log would
have been an artifact nobody could maintain.

**This is a siting decision, not a demotion, and it does not have to be
redone.** When a resident *can* write — phase 2's trace auditor, or Claudette's
adapter-side null-turn logging (her proposal, #custodian seq 501) — it appends
to its own file in its own volume and `collect` harvests it, exactly like the
sources below. The forward path needs no rework.

## Shape

One JSON object per line, append-only:

```json
{"ts": "2026-07-22T16:29:33Z", "ts_known": true,
 "logged_at": "2026-08-03T21:04:05Z",
 "source": "gable-summon", "subject": "res-gable",
 "kind": "timeout", "detail": "…session timed out after 300.0s",
 "evidence": {"file": "…/gable.log", "line": 179},
 "fingerprint": "a1b2c3d4e5f6a7b8"}
```

`kind` is the small stable taxonomy you grep at 3am: `truncation`,
`null_turn`, `refusal`, `timeout`, `session_failed`, `model_drift`,
`transport`, `crash`, `other`.

**`refusal` vs `null_turn`** — a safety classifier declining a request produces
the same empty reply as a token-wall death, so `refusal` is matched *first* or
the cause is stripped off. Three of Claudette's were filed as bare `null_turn`
before that pattern existed (2026-08-04).

**`ts` vs `logged_at`.** `ts` is when the fault happened; `logged_at` is when
we saw it. Claudette's adapter log carries no timestamps, so her events have
`ts: null` / `ts_known: false` and print as `seen <logged_at>`. They are
deliberately **not** back-filled with the collection time — thirty events
sharing a fabricated clock read as an incident that never happened.

## Use

```
errorlog.py collect                     # harvest (what the timer runs)
errorlog.py tail --days 7               # read it back
errorlog.py tail --kind truncation      # just the token-wall deaths
errorlog.py tail --subject res-gable --json
errorlog.py record --source keyboard --kind other --detail "…"
```

`record` is the universal writer — keyboard, cron job, future auditor, or a
human all append the same shape through it.

## The privacy rule — read before adding a source

Claudette's adapter log carries **whole conversations** on DEBUG lines. A
collector that copied matched lines verbatim would siphon chat content into a
house-level file with a different readership.

So each source declares `redact`. A redacted source contributes **only the
matched signature** (`stop_reason=max_tokens`) plus file and line — never the
surrounding text. `detail` is capped at `DETAIL_MAX` either way. **When you add
a source, decide its redact flag first.** There is a test that fails if a
redacted source ever leaks a line body.

## Idempotence

The timer runs `collect` every 10 minutes forever, so double-appending is the
failure mode that matters:

- **Watermark (primary)** — per source, `(inode, offset)`. A changed inode or a
  shrunk file means rotation, so the offset resets rather than silently
  skipping everything written since.
- **Fingerprint (backstop)** — covers a lost or reset state file. Anchored on
  the timestamp where there is one, and on `inode+line` where there is not, so
  a rotated file's line 3 cannot collide with the old file's line 3.
- Only complete lines are consumed: a half-written last line is left for the
  next pass rather than half-parsed.

## Install (keyboard)

```
sudo install -m 0644 harness/errorlog/disjorn-errorlog.service /etc/systemd/system/
sudo install -m 0644 harness/errorlog/disjorn-errorlog.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now disjorn-errorlog.timer
```

Nothing here is privileged: it reads files plink can already read and writes
one file plink owns (0640), in a directory systemd creates via `LogsDirectory=`
— the same mechanism behind `/var/log/disjorn-broker/audit.jsonl`, so no
install step has to mkdir as root. No broker call, no socket, no `/etc`, no
container.

## Sources today

| Source | File | Redact | Catches |
|---|---|---|---|
| `gable-summon` | `…/res-gable/resident-home/logs/gable.log` | no | model drift, timeouts, session exits, WS failures |
| `claudette-adapter` | `…/res-claudette/resident-home/logs/disjorn_bot.log` | **yes** | truncations, null turns, refusals, crashes |

## Known gaps

- **Claudette's log has no clock.** Everything from it is `ts_known: false`.
  The real fix is adapter-side — her seq-501 proposal — and lands in her area,
  not here.
- **Not wired to alerting.** This is a ledger, not a pager. The RAID temp
  alarm (`/usr/local/bin/raid-temp-log.sh`) is the pattern to copy if a kind
  ever deserves a nag.
- **The broker audit is not a source.** Denials are already recorded in
  `audit.jsonl` and surfaced by `query-own-audit`; duplicating them here would
  make two ledgers disagree. Revisit only if that stops being true.
