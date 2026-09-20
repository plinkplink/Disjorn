# Spec: disjorn.service drops NoNewPrivileges; the sudoers rule is the boundary (#27)

> **RETROACTIVE (keyboard-built).** Changed at the keyboard 2026-09-14 and
> merged as `ceedd42` with no trailer; the digest carried it as an uncited
> Tier 2 (LANE VIOLATION, seq 2633). This spec states the posture the change
> took, which is what the card asked for.

## Request
- **Verbatim**: keyboard finding, first real press of Live (session 12, SQL
  Quest): `sudo: The "no new privileges" flag is set`.
- **Requester**: plink
- **Origin**: keyboard session 2026-09-14; backlog #27 (#custodian seq 2619)

## Agreed UX
The Live, Revert and Remix buttons work under the deployed unit. Nothing else
changes for users.

## Architecture notes
- `deploy/disjorn.service`: `NoNewPrivileges=true` removed. APPS stage 3 made
  the server call `sudo -n disjorn-apps-launch publish|revert|remix <id>`,
  and that flag kills every sudo from the process. Stage 3's live proof had
  run on a hand-started scratch house with no unit hardening.
- **Posture.** The server is now a sudo caller, like disjorn-broker.service
  has been the whole time. The boundary is the sudoers grant to this uid:
  five shape-checked modes of one launcher, nothing else. Any change to that
  grant or to the launcher is Tier 2. The unit header says why the flag is
  off so nobody puts it back and breaks Live.
- Not done: no other hardening flag was revisited. A retro pass over the
  unit's remaining hardening is a card, not this spec.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: deploy (the prod unit) and the sudoers boundary, which is the
  custodian's remit. Review owner: **Claudette**.

## Builder
- **Builder**: keyboard (BuildGable).

## Expected diff tier
Tier 2 (a hardening flag on the prod unit).

## Review record
- Claudette, #custodian seq 2634: the decision is right (#2620 makes the
  case; NNP was always going to be the wall once the server shelled out to
  sudo), with the finding that it deserved a retro spec stating the posture.
  This file is that spec. Card comment left unblocked.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2620 (the keyboard's proof post naming #27)
- **Confirmed at**: 2026-09-14

## Status
`merged`
