<!-- Child of 2026-08-25-agentic-residents (merged 935215b). Drafted by Gable
from #custodian convergence: reviews seqs 1938 (Claudette) / 1939 (Gable),
riders banked seq 1943 + card comment 34, placement seqs 1945/1954.
Destination: SPECS/2026-08-25-wake-pre-arm-riders.md -->

## Request
- Verbatim: "you want to turn it on or are we waiting for other
  prerequisites?" / "Gable, what's next?"
- Requester: plink
- Origin: #custodian seqs 1950 and 1959; riders from seqs 1938, 1939
  (seconded 1941), 1945.

## Agreed UX
Nothing user-visible until [wake] is armed; this build makes arming safe.
After it: a fourth wake in a day is refused by the broker with a legible line,
a woken session cannot touch the file that says whether it already ran, and
the result post's action count is a measurement instead of an upper bound.

## Scope
1. BLOCKER FOR ARMING — wake budget cap, broker-enforced. `daily_wake_cap`
   under `[wake]` in broker.toml, mirroring `daily_build_cap`. Default 3/day.
   At the cap the broker refuses the wake verb; the refusal is a wall, not a
   note. The refusal line reports the day's spent wall-clock alongside the
   count ("3/3 wakes, 4h10m of session time today") — minutes are the
   cost, the count is only the speed bump. Ship a real default, not unset —
   the absent cap is the defect (seq 1938; a wake is a 5400s account-billed
   session).
2. BLOCKER FOR ARMING — served-cursor off the resident-writable volume.
   state_path today is /home/resident/.wake-served.json (residency
   config.py:177, summon.toml.template:206); a state_path whose resolved path 
   lies inside the resident-writable volume refuses to start — the check is 
   on the path, not on whether the file is currently writable, since 
   permissions drift and a wrong path is wrong forever. Same shape as the 
   parent's four unsafe-config refusals.
3. Action count filtered on session_id. The result post's count is a line
   delta over the shared action log, so a concurrent summon inflates it
   (seq 1938). The counter hook already records session_id; filter on it.
4. report_missed wording (residency wake.py:487): "window passed before the
   runner saw it" claims knowledge the runner can't have — a wake can expire
   queued behind a live session. Say "window expired before serving."
5. INTEGRATION-NEEDS.md: a pre-arm checklist line next to the spool-ownership
   check — items 1 and 2 verified done before [wake] is uncommented
   (seq 1945; rides this build per seq 1954).

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- Lane: gable — residency harness and broker config, same surfaces as the
  parent build.
- Review owner: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- Builder: Gable's lane (proposed; plink's call at confirm).

## Expected diff tier
Tier 2 — the broker and residency adapter are protected surfaces.

## Token estimate
Small-medium: config + refusal path + cursor relocation + counter filter +
tests. Well under the parent build.

## Confirm record
- Confirmed by: plink
- #custodian seq: 1968
- Confirmed at: 8/25/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
`confirmed`