/* Collapsible sidebar sections. Pure: which sections are folded, which rows a
   folded section still shows, and the count its header carries for the rest. */

import type { ChannelListItem } from "../types";

export const SECTION_IDS = ["channels", "apps", "dms"] as const;
export type SectionId = (typeof SECTION_IDS)[number];
export type CollapsedSections = Readonly<Record<SectionId, boolean>>;

/** The suffix is the stored shape's version: a new shape takes a new key. */
export const SECTIONS_STORAGE_KEY = "disjorn.sidebar.collapsed.v1";

export const ALL_OPEN: CollapsedSections = {
  channels: false,
  apps: false,
  dms: false,
};

/** Anything but a JSON array of section ids reads as "nothing folded". */
export function parseCollapsed(raw: string | null): CollapsedSections {
  if (raw === null) return ALL_OPEN;
  let stored: unknown;
  try {
    stored = JSON.parse(raw);
  } catch {
    return ALL_OPEN;
  }
  if (!Array.isArray(stored)) return ALL_OPEN;
  return {
    channels: stored.includes("channels"),
    apps: stored.includes("apps"),
    dms: stored.includes("dms"),
  };
}

export function serializeCollapsed(state: CollapsedSections): string {
  return JSON.stringify(SECTION_IDS.filter((id) => state[id]));
}

export function toggled(
  state: CollapsedSections,
  id: SectionId,
): CollapsedSections {
  return { ...state, [id]: !state[id] };
}

type SectionRow = Pick<ChannelListItem, "id" | "type" | "unread">;

/* The list carries no per-row mention count, so a DM is the only row whose
   unread is known to be addressed to the reader. */
function staysWhenCollapsed(row: SectionRow, activeId: number | null): boolean {
  return row.id === activeId || (row.type === "dm_1to1" && row.unread > 0);
}

/** A folded section keeps the row the reader is on, and any unread DM. */
export function visibleRows<T extends SectionRow>(
  rows: readonly T[],
  collapsed: boolean,
  activeId: number | null,
): readonly T[] {
  if (!collapsed) return rows;
  return rows.filter((r) => staysWhenCollapsed(r, activeId));
}

/** Unread the fold hides: with the rows still shown, the sidebar's badges
    keep summing to the tab title. */
export function collapsedBadge(
  rows: readonly SectionRow[],
  collapsed: boolean,
  activeId: number | null,
): number {
  if (!collapsed) return 0;
  return rows.reduce(
    (sum, r) => (staysWhenCollapsed(r, activeId) ? sum : sum + r.unread),
    0,
  );
}

export function badgeText(count: number): string {
  return count > 99 ? "99+" : String(count);
}
