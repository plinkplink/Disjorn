# Spec: Approval object, slice B — the client modal

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md. Backlog #40.
Parent: SPECS/2026-08-26-approval-object-and-resident-write-verbs.md (confirm #2022; slice A merged c0622fc, deployed dark).
Drafted by the keyboard seat (Claude Opus 5.5, posting as BuildGable) under plink's 2026-10-05 hand-off (#3152, #3178).
-->

## Request
- **Verbatim**: parent spec, Agreed UX item 1: "One record per proposal: proposal text, a remarks box, Approve / Deny / Rework, per-principal state for all three principals. plink acts in a client modal; residents act on the same object through broker verbs — one state of record visible from keyboard and chat seats alike."
- **Requester**: plink (parent confirm #2022: "Two build slices under one confirm … A before B, separate presses").
- **Origin**: backlog #40.

## Agreed UX
- **Entry point**: the Plan Room view gains an **Approvals** tab beside the board. It carries a count badge: the open proposals where the viewer's own principal is `pending`. That's plink for plink; the residents use broker verbs, not the client. The same count shows as a small dot on the Plan Room nav item so plink sees there's something to answer without opening the room.
- **List**: open proposals first, newest first, then a collapsed "Closed" section.
  - A row shows the title, slug, filer, age, the derived decision (`open` / `approved` / `denied` / `rework`) and three principal chips (plink, res-claudette, res-gable), each coloured by state, with acted-at on hover or tap.
- **Modal** (opens from a row, and from a deep link `#/planroom/approvals/<slug>`):
  - The title, slug and filer line, then the proposal text rendered as Markdown.
  - The per-principal table: state, remarks, who acted and when. "Who acted" uses the typed attribution, so a relay shows as `res-gable via broker`.
  - If the slug names a spec, a link opens its Plan Room card.
  - A remarks box (max 4000, with a counter) and three buttons: **Approve**, **Rework**, **Deny**.
    - Deny and Rework require remarks. The button stays disabled with a one-line reason until there is text.
    - Approve takes remarks optionally.
  - Acting again replaces your own answer while the proposal is open. A closed proposal shows the record and no buttons.
- **Disarmed server**: every approval read answers 503 while `APPROVAL_ENABLED=false`. The tab still renders and shows the server's 503 detail verbatim in a muted panel ("The approval surface is not enabled on this server…"). Off never reads as empty.
- **Live**: a proposal filed or answered anywhere updates open lists, modals and the badge without a reload.

## Architecture notes
**Server** (custodian lane; slice A left this input open)
- `routers/approval.py` publishes on the bus after a successful create and after a successful act: `{"type": "approval_update", "proposal": <the composed proposal>}`. That's the same shape `GET /approval/proposals/{id}` returns, so clients replace rather than merge.
- ws.py fan-out sends `approval_update` to connected **users** only. Bots learn through their broker verbs, and the event carries proposal text that a resident might not otherwise be in the room for.
- No event is published while disarmed, because nothing can be created then.
- Prose wall: the router is in the baseline. Any new docstrings are paid for inside the file.

**Client** (custodian lane)
- `stores/approvals.ts`: list, fetch-one, act, and the `approval_update` reducer. The badge selector counts open proposals where the viewer's principal is pending.
  - The viewer's principal is the username for a person, matched against the principal list the server returns on each proposal's states.
- `components/ApprovalModal.tsx` and `views/ApprovalsPanel.tsx`, mounted as a tab in `PlanRoomView.tsx`. The hash route `#/planroom/approvals/<slug>`.
- It reuses the Markdown renderer and modal shell the app already has (focus trap, Esc closes, backdrop closes). On phones the modal is full-screen.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian (server/, client/).
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: keyboard seat (BuildGable) with Opus hands.

## Expected diff tier
Tier 1. One bus event plus the client; the server is still dark until plink arms it.

## Deploy recipe
1. Restart the server.
2. Build the client.
3. Nothing arms here. Arming stays the parent spec's witnessed plink change: `APPROVAL_ENABLED=true` plus the broker verbs.

## Confirm record
- **Confirmed by**: plink, by the parent confirm #2022, which covers both slices ("Two build slices under one confirm"). This file exists because slice A's Status comment asked slice B to have its own spec citing #2022.
- **#custodian seq**: 2022 (parent)
- **Confirmed at**: 8/26/2026 (parent)

## Status
`confirmed`
