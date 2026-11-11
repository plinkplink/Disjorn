/* Which sidebar sections are folded on this device. It lives outside stores/
   because a change of account must not reset it. */

import { create } from "zustand";

import {
  ALL_OPEN,
  SECTIONS_STORAGE_KEY,
  parseCollapsed,
  serializeCollapsed,
  toggled,
} from "./lib/sidebarSections";
import type { CollapsedSections, SectionId } from "./lib/sidebarSections";

function load(): CollapsedSections {
  try {
    return parseCollapsed(localStorage.getItem(SECTIONS_STORAGE_KEY));
  } catch {
    return ALL_OPEN;
  }
}

interface SidebarSectionsState {
  collapsed: CollapsedSections;
  toggle: (id: SectionId) => void;
}

export const useSidebarSections = create<SidebarSectionsState>()((set, get) => ({
  collapsed: load(),
  toggle: (id) => {
    const collapsed = toggled(get().collapsed, id);
    set({ collapsed });
    try {
      localStorage.setItem(SECTIONS_STORAGE_KEY, serializeCollapsed(collapsed));
    } catch {
      // Private mode or full storage: the choice lasts until reload.
    }
  },
}));
