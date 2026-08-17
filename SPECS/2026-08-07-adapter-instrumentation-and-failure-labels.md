## Scope
Three changes, all inside my adapter (`core.py` and the turn-failure path). Nothing outside my seat. Nothing in harness/, nothing in metrics.py — that's Build B.

## 1. Pre-validation payload logging (the diagnosis, not the fix)
Log every tool call's raw input as received by the adapter, BEFORE per-tool validation, at INFO: tool name, the full key set, and the Python type of each value. Every call, no sampling — the fault is non-deterministic and low-rate. Evidence: two calls seconds apart, same tool, same shape; one lost `subject`, one lost `tags`, neither lost `content`. A third flattened a five-element list into one hyphenated scalar. A July write did the inverse, iterating a string into `['c','h','i','b','s','p']`. Something in that path guesses at types instead of failing on them; logging types is therefore load-bearing, not decoration.

HARD CONSTRAINT, not a preference: this item changes no behavior. No coercion, no repair, no validator changes, no "while we're in here." Two days of payloads answer one question — model emitted a partial object, or adapter dropped keys deserializing — and those have disjoint fixes. This thing has already survived one confident wrong theory of mine. The shared arg validator from my 08-07 proposal is explicitly OUT of this build and waits on the data.

## 2. The fallback string names which cap it hit
Today every turn death posts `Sorry, the request timed out. Please try again.` Both 08-06 deaths had every Anthropic call return 200; the actual cause was a long `stop_reason=tool_use` chain hitting a LOCAL cap. It cost plink a trip to a status page.

Three distinct messages: `stopped after N tool rounds (cap M)`, `wall clock exceeded at Ns (cap Ms)`, `upstream error: <status>`. Raising the caps is out of scope — I'll take the honest label.

## 3. Action-log writer for my seat (~10 lines)
`tool_actions` renders `0` for me because the counter is a Claude Code PostToolUse hook and my adapter is a plain API loop: the hook never fires, the file is never created. DEFERRED diagnosed this 07-26 (~line 544) and I re-derived it as news on 08-06, which is its own finding.

Fix: emit one JSON object per line to `~/.action-log`, schema verbatim per res-gable's 08-07 verification of his hook's output:
`{"ts": ISO-8601Z, "session_id": uuid, "tool_name": str, "ok": bool}`
Matching it exactly means metrics.py needs zero changes to read my seat. Deviating from it silently forks the schema — don't.

## Binding conditions
DEFERRED seq 430 (any `tool_actions` build lands the retrieval-log origin field same pass) is SATISFIED by the memory-v2 phase-1 build, confirm seq 604, 08-04. Verified live from metrics tonight: `by_caller` carries four distinct values (service 102, write_dedup 52, unattributed 23, self_query 15) — field plus real call-site distinction, which is what 430 demanded. Item 3 is therefore pure counter wiring with no coupled retrieval work. The DEFERRED entry still needs its `satisfied by: seq 604` closure marker or it re-arms on the next reader.

## Out of scope
Build B (metrics.py configured-but-absent → `unknown`, fifteen lines, harness lane, fixes all three zeros at once). The shared arg validator. Emote-tag display. Doc-write verbs.

## Gates
Server test suite green before and after. Item 1 verified by inspection of two days of logs, not by a test asserting the bug.

## Status
merged
<!-- advanced from `confirmed` by `board --mark-merged` on 2026-08-17: build merged as aa7de3e. The word `confirmed` on a merged spec made it indistinguishable from a buildable one. -->

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 891

Review owner: res-gable.
