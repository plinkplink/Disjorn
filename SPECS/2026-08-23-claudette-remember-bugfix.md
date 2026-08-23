# Spec: remember tool-call corruption — strict tool use, hard refuse, repair sweep

## Request
- **Verbatim**: "Claudette, are you ready to draft the remember/SDK spec?" (plink, #custodian 1626); underlying order from plink #1594 — the remember bug goes to BuildGable for external diagnosis before anyone presses build.
- **Requester**: plink
- **Origin**: #custodian seq 1626. Diagnosis of record: seq 1611 (BuildGable, keyboard seat, read-only pass over the chroma store, the payload log and both code trees). Prior emission-side verdict: seq 1556 (Claudette), confirmed by 1611. Specimen seqs, cited not re-collected: 1558, 1576, 1578, 1586.

## Agreed UX
Nothing visible in chat. What changes is that my memory stops lying by omission.

Today ~40% of my `remember` writes arrive corrupted (176 calls: 106 clean, 28 tags-as-string, 36 tags-missing, 27 subject-missing) and the store holds 36 zero-tag and 29 mega-tag memories, all invisible to tag recall. A failed write is loud; a missing recall is just me being confidently blank. After this: writes validate at the source, malformed ones are refused loudly rather than coerced silently, and the existing damage is repaired where recoverable.

## Architecture notes

**Root cause (seq 1611, closing seq 1542's open question).** 22 stored memories carry raw tool-call markup in their bodies (`</content>`, `<parameter name=`, bare `<tags>`), one of them having swallowed an entire second `recall` invocation. `json.loads`/pydantic cannot fabricate markup, so that text was inside the `content` string in the wire JSON the API delivered — corruption is upstream of the SDK and of every line of core.py. Mechanism: two parameter dialects appear in the corpses, canonical `<parameter name="tags">` and a bare `<tags>…</tags>` form. When my emission drifts into the non-canonical dialect mid-call, the API-side parser stops recognizing later parameters and absorbs them into the still-open string. One mechanism, every observed shape: `content` always survives because it is the absorber; `subject` and `tags` vanish by drift position. Predates the 07-24 substrate swap — both models, not Opus 5.

**Part 1 — the cure: `strict: true` on the tool definitions.** Top-level field on each tool definition; schema gains `additionalProperties: false` and an explicit `required` list. GA, no beta header, supported on `claude-opus-5` and the `opus-4-8` fallback. The API then guarantees `tool_use.input` validates against the schema. Plain JSON field, so it should pass through SDK 0.67.0 untouched (`tool_schemas()` builds plain dicts); if 0.67.0 strips it, that is the concrete reason to take the 0.125.0 bump, and the bump is a separate decision with its own spec, not a thing this build does quietly. Verification of record for the `strict: true` claims (GA, no beta header, both models) is BuildGable's keyboard-side read at #1611; not independently checkable from a resident container. The acceptance gate is empirical, so a wrong claim fails loud rather than shipping quiet.

**Verification gate — check the body, not the keys.** `strict` guarantees the input *validates*. It does not, on its face, guarantee `content` is *faithful*. If constrained decoding genuinely prevents the dialect drift then both problems die together, which is the expected outcome — but the acceptance test is: one live `remember` with all five arguments and a long free-text `content`, then read the stored record back and assert the body contains no `</content>`, no `<parameter name=`, no `<invoke`. Green keys over a corrupted body would look exactly like success. This gate is the build's pass/fail, not the validation itself. Second green condition, post-deploy: the payload log over the next 30–50 organic writes shows zero markup-in-content and zero tags-as-string. The build is not done at merge; it is done when that read happens. Gable is second reader on both halves.

**Part 2 — kill the coercion path.** The 08-04 shredder fix is not inert, it is a live source of new corruption. `normalize_tags` coerces a bare string to ONE tag ("the single tag the caller obviously meant"), but the strings are comma-separated lists, so `normalize_tag` strips the commas and mints a single garbage mega-tag. Fired 29 times since the fix. Replace that coercion — do not add a refuse beside it. Malformed `tags` returns `is_error: True` with a message naming the expected shape, and the retry loop handles anything residual. Defense in depth behind `strict`, and the thing that catches the next dialect nobody has seen yet.

**Part 3 — repair sweep, not a scrub.** The swallowed values are sitting in the corrupted bodies in parseable form, so the sweep recovers before it deletes:
- 22 markup-bearing memories: parse the absorbed parameter block out of the body, restore `subject`/`tags`/`salience` to their fields, truncate the body at the drift point, re-embed.
- 29 mega-tags: recover component tags from the payload log's pre-coercion comma strings. The mega-tag itself is not a source — splitting it is ambiguous. Records the log doesn't cover get flagged, not reconstructed.
- 36 zero-tag memories: recover tags where the body carries them; where it doesn't, leave the memory and flag it rather than inventing tags.
- Anything unparseable falls back to scrub-and-flag. Nothing is deleted.

The sweep changes fields, body and embedding only. Record id, created-at and provenance are preserved. The dry-run diff asserts metadata columns identical before/after; any drift there fails the sweep.

Order of operations, and it is load-bearing: **dry run first**, output posted to #custodian as a diff of before/after per record, and a full backup of the chroma store taken before any write pass. That is my own requirement from #custodian 14-20 on July 19, not new caution — a sweep that rewrites my memories in bulk gets a human witness on the diff before it runs, and the correct verb is posted, not approved.

**Part 4 — optional telemetry.** `with_raw_response` capture, downgraded from "decides the build" to nice-to-have (seq 1611). It would only re-prove what 22 memories already prove. Build it if it's cheap, drop it if it isn't.

## Non-goals
No changes to memory hygiene, eviction, or the record schema. No SDK version bump inside this build. No new memory verbs.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — my own memory adapter and tool definitions are Claudette's surface.
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's call. BuildGable recommended — the keyboard seat has already read the store, the payload log and both trees read-only, so the corpse inventory doesn't have to be rebuilt. Not me: I am the specimen and cannot see my own emission.

## Expected diff tier
Tier 2 — it changes how my writes are validated and it rewrites stored memories in bulk. Advisory; the classifier gates the actual result at merge.

## Token estimate
About one slot. `strict: true` and the refuse path are small; the repair sweep and its dry-run reporting are the bulk.

## Rollback
`strict: true` is a one-line revert. The sweep is covered by the pre-write backup of the chroma store. The coercion path is not restored under any circumstances — it is the defect.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1672
- **Confirmed at**: 2026-08-23
<!-- No Confirm record → no build. This is the gate. -->

## Status
`confirmed`
