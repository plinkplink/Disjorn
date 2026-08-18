# Spec: Credential routing & halt protocol — all seats on the Max account

> **AMENDED 2026-08-14 (plink): the routing table is reversed.** As written on
> 08-05 this spec put chat seats on metered API keys and build loops on the Max
> account, and chose that split *for failover isolation* — §1's own words were
> that a Max limit would halt the loop "and leave chat seats untouched, because
> they never shared a credential." plink then read the actual meter: Claudette's
> chat seat was spending ~$40/day on read-heavy days. **Every seat of every
> resident now runs on the Max account.** Matters of the account and its cost
> are plink's alone to decide (standing rule, reaffirmed in chat 2026-08-14).
>
> What the reversal deliberately gives up: the isolation. A Max rate limit is
> now a total outage — both residents, both seats at once — not a build pause.
> Two consequences are already enforced in code: **no seat may spend the
> metered key** (`_seat_metered_fallback=refuse` in BOTH wrappers, so an
> expired token fails loud instead of quietly re-billing the key), and the
> deliberate exception is `RESIDENT_METERED_OK=1` at launch — host-side env,
> never /config, loud on every use. §3's halt protocol is therefore no longer
> a nicety but the difference between a diagnosable outage and an evening.
> §1 below is kept as written for the record of what was reversed and why it
> was originally chosen.

Drafted by Gable (#custodian seq 694), amended by Claudette (seq 696), delta
v2→v3 by Gable (seq 705), committed by the keyboard seat with the seqs filled.

## Request
- **Verbatim**: "OK, we need to get the API-Claudette/Max-agents + cutoff instructions into the spec. Gable, what's your take?"
- **Requester**: plink
- **Origin**: #custodian seq 692, 2026-08-05

## Agreed UX

1. **Routing by seat.** Conversational seats run on per-seat API keys (cheap,
   bursty, hard spend cap, 30-second rotation). Build/agent loops run on plink's
   Max account. The seat split is the routing table; it is also the failover
   isolation — a Max limit halts the loop and leaves chat seats untouched,
   because they never shared a credential.

2. **The credential never enters the talking container.** Broker holds the
   OAuth credential store. Resident containers get `ANTHROPIC_BASE_URL` pointed
   at a local proxy; the proxy injects real auth outbound. In-container env
   holds a dummy token whose disclosure costs nothing. OAuth browser/2FA flow
   runs on the host, plink's hands — a fortnightly chore, not an interface. No
   interface gets built until the chore has annoyed plink three times
   (Claudette's rule, adopted).

   **The API-key fallback lives broker-side as a second route in the proxy —
   never as a credential inside the container, which would half-delete the
   dummy-token claim.** The proxy holds both credentials and injects exactly
   one; which one is a route flip made by plink's hand on the host. Silent
   fallback is impossible *by construction*: the resident cannot reach the
   routing table it would need to flip. *(Claudette's amendment 1, seq 696;
   ruled by plink, seq 704: "her route flip is good structure. Let's go with
   that.")*

   **Upstream is pinned.** The proxy allowlists Anthropic API hosts, refuses
   everything else, and logs refusals loudly — an auth-injecting proxy with an
   open upstream is an open relay for the Max token. *(Claudette's amendment 2,
   seq 696; adopted outright by Gable, seq 698.)*

3. **Halt protocol — one artifact, three triggers.** Max limit hit, action
   budget tripped, or resident declares out-of-depth: identical response. Stop
   at the merge-gate boundary (finish or abandon the unit of work, never
   suspend inside one), write a passdown (done / half-done / next), post one
   line in channel with the reset time, standby. **No polling, no retry loop,
   no silent resume, no silent key-fallback.** Restart is plink's call, made
   with the passdown and his other Max workload in front of him.

   *Recorded against plink's later ruling, not his earlier one.* Seq 681 said
   "standby until the limit clears and then keep going"; seq 683 and 697 said
   "plink restarts it" — *"I need to look at the account before restarting any
   work that ate the whole budget."* The later ruling governs. The same rule
   kills the tempting fallback: a loop that silently fails over from Max to the
   API key has converted "your account said not now" into "spend your API money
   instead", which is exactly the decision plink reserved.

4. **Limit visibility.** Anthropic's limit message reaches the resident
   verbatim — body and reset timestamp, not a swallowed adapter exception. If
   plumbing cannot deliver it in-turn, the harness posts it: one line down, one
   line back. **Rate-limited must never be indistinguishable from wedged** —
   the failure mode already on the board as `No response generated.`

5. **Two meters, two owners.** Max limits are plink's account saying *not now*;
   the daily action budget is the resident's own legibility counter.
   Limit-blocked turns burn no budget; a limit is not evidence of overspend.
   Both meters print nightly; neither enforces until a week of real shape is
   watched (`daily_action_cap` stays null pending seq-599 actor attribution —
   unchanged). plink's steer, seq 683: keep the deny line high; the counter is
   for legibility, not restraint.

## Architecture notes

- Proxy lands broker-side (same box as the credential store); the container
  change is one env var plus the dummy token. Same shape as every privileged
  verb: secret outside, socket inside.
- **Per-seat metering falls out of the proxy for free** — it sees every request
  per seat. **Per-container, not per-actor: this instrument does NOT close
  seq 599 and must not be recorded as closing it.** A daemon inside a
  resident's container still meters as the resident. *(Claudette's third note,
  seq 696 — right instrument, wrong hole.)*
- Conditions carried from `harness/KEYBOARD-NEXT.md` §3b, all binding:
  dedicated token via `claude setup-token`; **prove revocation works before the
  cutover** — a credential you cannot demonstrably revoke does not belong
  behind the proxy either; KB-D13 closed properly (per-seat keys, revoking one
  must not kill both); on any 401 check token *shape* first (`sk-ant-oat01-`
  prefix, length, whitespace) before assuming revocation.

### KB-D6 is re-scoped by this spec — the sequencing inverts

KB-D6's own text says the risk exists because "Claude Code authenticates with
it, so any code the session runs reads it from `/proc/self/environ`", and the
only countermeasure it could offer was a token-shaped-string scanner on
outbound posts — an averted-eyes wall of exactly the kind this house does not
build.

**A broker-held credential plus an injecting proxy deletes the premise rather
than mitigating it: the container authenticates with a worthless dummy, and a
resident cannot speak what it does not hold.** So "the cutover is gated on
KB-D6" inverts to **the cutover waits on the proxy, and KB-D6 becomes the probe
that verifies the wall holds.** *(Gable, seq 694; accepted by Claudette,
seq 696, who wrote the proxy and whose release condition this changes: from
"I'm on the API key until KB-D6 lands" to **"proxy in place and probe green"**.)*

Probe scope, from inside the container: confirm the real token is unreachable
via env, `/proc`, config mounts, and the proxy's own interface; **attempt the
open relay** (a request at an attacker host with the base URL already set); and
**attempt to make the proxy echo its own injected auth header back in a
response.** All must fail. KB-D7(a)'s unmasked `gable-key` is the same family —
the proxy shape fixes the class, and the probe should sweep for stragglers.

## Lane → Review owner (DETERMINISTIC)

- **Lane**: cross-lane — see split.

## Builder (USER PREFERENCE)

- **Builder**: keyboard seat — not preference but necessity: `start-build` has
  not flipped, so no resident builds. Note the irony that this build is itself
  upstream of the proving window.

## Cross-lane split

- **Applies**: yes
- **Surfaces by lane**:
  - broker/proxy, credential store, limit-message relay → review owner **plink**
  - Claudette's container env/config + her release-condition re-scope → review owner **Claudette**
  - Gable's container env/config (same cutover, second in line) → review owner **Gable**
- **Split agreed in #custodian**: seq 696 (Claudette), seq 698 (Gable), seq 704 (plink)

## Expected diff tier

Tier 2 — credential surface is protected, two-way review. **This classification
is only meaningful once `SPECS/2026-07-26-approval-tiers.md` has a non-empty
Confirm record**, which is why the tiers confirm sequences ahead of this build.

## Token estimate

~150k. Burns once on a confirmed build.

## Confirm record

- **Confirmed by**: plink
- **#custodian seq**: 704
- **Confirmed at**: 2026-08-05

The three lines above are the two fields the broker's gate parses plus the
date; everything below is the record of HOW it was confirmed, which is the part
worth reading. Both are kept because they answer different questions — the gate
asks "may this build start", a person asks "what did each seat actually agree
to". Filled 2026-08-14 at the keyboard: the sign-off had existed since 08-05 in
the per-seat form below, but `parse_confirm_record` reads only
`- **Confirmed by**:` and `- **#custodian seq**:`, so mechanically there was no
confirm record and `start-build` would have refused a spec every seat had
signed.

- **Claudette** — #custodian seq 696, 2026-08-05. Accepted **with two
  amendments**, both adopted into the text above (route-flip fallback; pinned
  upstream), plus one labelling note (proxy metering does not close seq 599).
  Recorded as an amendment set rather than a bare yes, per her standing rule
  that a gate logging only approvals is a receipt and not a record.
- **plink** — #custodian seq 704 ("her route flip is good structure. Let's go
  with that.") for the route-flip amendment, and seqs 683/697 for the
  restart-is-plink's-call ruling recorded in §3.
- **Gable** — #custodian seq 694 (author) and seq 705 (adoption of Claudette's
  full amendment set).

## Status

confirmed
<!-- WHAT REMAINS (keyboard note, 2026-08-18, so a builder does not rebuild the
half that exists): §1 routing and §5 no-silent-key-fallback are DONE at the
keyboard — reversed per the 08-14 banner, enforced in both wrappers
(`_seat_metered_fallback=refuse`, `RESIDENT_METERED_OK=1`), commits 2985982,
f881405, 6b6979f. STILL TO BUILD, and what this confirm now covers: §2 the
credential proxy (the token never enters the talking container; pinned
upstream; route flip host-side), §3 the halt protocol (one artifact, three
triggers, passdown, plink restarts), §4 limit visibility. Since the 08-14
reversal put every seat on one token, §2 and §3 are load-bearing, not
niceties (DEFERRED.md 2026-08-14). -->
