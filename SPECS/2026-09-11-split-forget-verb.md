## Problem
forget is one verb with two meanings, discriminated by the PRESENCE of supersede_content: absent means hard-delete, present means supersede. That field is the longest free-text string in the call, and the long string is exactly what generation drops (the "swallow tail": 08-23 specimen 404bbd5d, a phantom call whose only field was a newline; 09-10 specimen, keys=['memory_id','subject','salience','confidence','confirm_delete'] with supersede_content simply absent). So the swallow silently converts "update this memory" into "delete this memory". A guard in memory/tools.py refuses replacement-fields-without-content rather than guessing, and it has now prevented eleven hard deletes across four incidents. The guard is the only thing between the swallow and data loss, and it works because it distrusts the schema — which is the tell that the schema is wrong.

Because supersede_content is optional by design, a swallowed call is a VALID strict call. Strict tool use cannot refuse it, and _payload_anomaly (which fires only on a missing REQUIRED key) missed the shape for eighteen days.

## Change
Two verbs, each with its meaning in its required list:
- supersede_memory(memory_id, supersede_content, [subject required], tags?, salience?, confidence?) — supersede_content in "required". A swallow is now a missing required field: strict may refuse to emit it at all, and _payload_anomaly catches whatever slips through.
- delete_memory(memory_id, confirm_delete=true) — confirm_delete required and the ONLY other field. Any replacement field present is a refusal, not a delete.
- forget stays registered for one release as a refusing shim: any call returns one sentence naming the replacement verb and which one the args look like. A swallowed call from an old habit gets a sentence instead of a silence. Remove it on the release after.

Inheritance semantics are unchanged: supersede inherits tags/salience/confidence unless overridden; the chain is preserved; delete stays permanent.

## Non-goals
Not changing the guard — it stays after the split, belt and braces, because the thing it defends against is me. Not changing remember. Not changing memory storage or the chain format.

## Tests
1. supersede_memory without supersede_content is refused as missing-required and raw-dumped by _payload_anomaly.
2. delete_memory with any replacement field present is refused.
3. delete_memory without confirm_delete=true is refused.
4. Supersede inherits tags/salience/confidence when omitted, overrides when passed, tags=[] clears.
5. forget (any args) refuses with the shim sentence and mutates nothing.
6. Chain integrity: superseded parent stays flagged, child links to it.

## Open item, not blocking
refused_shapes in core.py is per-turn, so recurrence across turns is invisible. Post-split I want the count keyed cross-turn on (tool, args hash) so we can measure whether the same supersede swallows again tomorrow. That is the diagnosis; the repeat-suppressor was the damage control. Separate card.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2526
- **Confirmed at**: 9/10/2026

## Status
`confirmed`