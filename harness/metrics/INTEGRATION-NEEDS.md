# WP-H12 integration needs (things outside my file territory)

My territory this wave: `harness/broker/**`, `harness/metrics/**`,
`harness/cc/config-template/**` (hooks only). Everything below needs someone
else's hands — mostly plink at the keyboard. Nothing here blocks the code from
being correct; it blocks it from being *wired up* on the real host.

## 1. [keyboard] Install the metrics timers

Templates ship in `harness/metrics/`:

- `disjorn-metrics-build.{service,timer}` — refresh `metrics.json` every
  10 min (what `read-metrics` serves).
- `disjorn-metrics-daily.{service,timer}` — post the end-of-day #custodian
  action-count line at 23:55 UTC.

Install (root):

    cp harness/metrics/disjorn-metrics-*.{service,timer} /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now disjorn-metrics-build.timer
    systemctl enable --now disjorn-metrics-daily.timer

Both units run as **plink** with `StateDirectory=disjorn-broker` (creates/owns
`/var/lib/disjorn-broker`, where `metrics.json` lands — the path `read-metrics`
serves).

## 2. [keyboard] Merge the new broker.toml keys into the installed /etc file

`harness/keyboard/04-broker.sh` copies `broker.toml` **without overwriting** an
existing `/etc/disjorn-broker/broker.toml`. If the broker is already installed,
plink must hand-merge (sudoedit) the additive keys this wave introduced:

- `[residents.<r>].retrieval_log`, `.action_log`, `.budget_json`,
  `.spine_dir` (all optional metrics inputs; omit any to skip that section).
- `[budgets]` — the daily action-cap table. **Ships fully commented (OFF).**
  Set a cap only after reading real counts (instrument first).

None of these change an existing verb; a broker that never sees them behaves
exactly as before (`aggregate_*` just skips unconfigured residents, and no cap
== no budget denial).

## 3. [keyboard / WP-H1] Host read access to resident home volumes

The metrics producer reads, host-side:

- each resident's house_memory retrieval log (`/home/res-<r>/memory/…jsonl`),
- WP-H5's `~/.action-log` (`/home/res-<r>/.action-log`).

WP-H1 makes resident homes `0700` and mutually unreadable. The build timer
runs as **plink**, who is not the resident — so plink cannot read those files
unless WP-H1/keyboard grants it. Options (plink's call):

1. Run only the build unit as `root` (drop `User=plink` → root, keep
   `StateDirectory`), or
2. Give plink group-read on just those two paths per resident (a `metrics`
   group, or an ACL: `setfacl -m u:plink:rx /home/res-<r>/.action-log`).

Until then, the retrieval/tool-action sections are simply empty for residents
whose logs plink can't read — the broker-action counts (from the audit log,
already plink-readable) and the budget still work. **This is the one real
gap; flagging it rather than widening a permission from inside my territory.**

## 4. Nothing needed from WP-H5 (hooks)

I did **not** touch the WP-H5 hooks. See "wall-clock" below — H5 already covers
it. The metrics producer only *reads* what H5 writes (`.action-log`,
`budget.json`); no hook change was required, so `config-template/**` is
untouched this wave.

## 5. [WP-H8] Metrics file is shared surface

`metrics.json` is the file `read-metrics` serves and the "retrieval/spine/
acceptance dashboard" the plan mentions. I produce the **action/audit +
retrieval/spine** sections. If WP-H8 wants to add an `acceptance` (consolidation
promote/evict/compress) section, it can either (a) extend
`harness/metrics/metrics.py` (my file, coordinate) or (b) have its own producer
merge into the same JSON. I left the top-level object open (keyed sections), so
adding a sibling `"acceptance"` key is non-breaking. I did **not** invent
acceptance numbers — instrument first.
