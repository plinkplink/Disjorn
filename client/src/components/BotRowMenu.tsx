/* Context menu for a bot's row in the member panel: its reply mode in this
   channel, and removal. Mounted only for people who may manage the channel's
   bots (UserPanel decides; the server's refusal is the wall).

   Rendered into document.body so the scrolling member panel neither clips it
   nor offsets its fixed position. */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";

import type { ReplyMode } from "../types";

const EDGE = 8;

export interface BotRowMenuProps {
  botName: string;
  /** Viewport point the menu hangs from (pointer, or the ⋯ button's corner). */
  at: { x: number; y: number };
  mode: ReplyMode;
  mentionOnly: boolean;
  onSetMode: (mode: ReplyMode) => void;
  onRemove: () => void;
  /** `restoreFocus` is false when the menu closed by a pointer elsewhere. */
  onClose: (restoreFocus: boolean) => void;
}

export function BotRowMenu({
  botName,
  at,
  mode,
  mentionOnly,
  onSetMode,
  onRemove,
  onClose,
}: BotRowMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState(at);

  // Keep the whole menu on screen: a right-click near the bottom-right corner
  // of a phone would otherwise hang it off the edge.
  useLayoutEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const { width, height } = el.getBoundingClientRect();
    setPos({
      x: Math.max(EDGE, Math.min(at.x, window.innerWidth - width - EDGE)),
      y: Math.max(EDGE, Math.min(at.y, window.innerHeight - height - EDGE)),
    });
  }, [at]);

  const items = (): HTMLElement[] =>
    Array.from(
      ref.current?.querySelectorAll<HTMLElement>(
        '[role^="menuitem"]:not([aria-disabled="true"])',
      ) ?? [],
    );

  useEffect(() => {
    const enabled = items();
    const checked = enabled.find((el) => el.getAttribute("aria-checked") === "true");
    (checked ?? enabled[0])?.focus();
  }, []);

  const onKeyDown = (e: ReactKeyboardEvent) => {
    const enabled = items();
    const i = enabled.indexOf(document.activeElement as HTMLElement);
    const go = (n: number) => {
      e.preventDefault();
      enabled[(n + enabled.length) % enabled.length]?.focus();
    };
    switch (e.key) {
      case "ArrowDown":
        return go(i + 1);
      case "ArrowUp":
        return go(i < 0 ? enabled.length - 1 : i - 1);
      case "Home":
        return go(0);
      case "End":
        return go(enabled.length - 1);
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        return onClose(true);
      case "Tab":
        e.preventDefault();
        return onClose(true);
    }
  };

  const choose = (next: ReplyMode) => {
    onClose(true);
    if (next !== mode) onSetMode(next);
  };

  return createPortal(
    <>
      <div
        className="picker-scrim"
        onClick={() => onClose(false)}
        onContextMenu={(e) => {
          e.preventDefault();
          onClose(false);
        }}
      />
      <div
        ref={ref}
        className="status-pop bot-row-menu"
        role="menu"
        aria-label={`${botName} options`}
        style={{ left: pos.x, top: pos.y }}
        onKeyDown={onKeyDown}
      >
        <div className="bot-row-menu-label" role="presentation">
          Replies to:
        </div>
        <button
          role="menuitemradio"
          aria-checked={mode === "mention"}
          className="status-option"
          onClick={() => choose("mention")}
        >
          <span className="bot-row-menu-check" aria-hidden>
            {mode === "mention" ? "✓" : ""}
          </span>
          When called by name
        </button>
        <button
          role="menuitemradio"
          aria-checked={mode === "always"}
          aria-disabled={mentionOnly}
          className={`status-option${mentionOnly ? " disabled" : ""}`}
          title={mentionOnly ? "This room is mention-only" : undefined}
          onClick={() => {
            if (!mentionOnly) choose("always");
          }}
        >
          <span className="bot-row-menu-check" aria-hidden>
            {mode === "always" ? "✓" : ""}
          </span>
          <span>
            Every message
            <span className="reply-mode-hint">
              {mentionOnly
                ? "This room is mention-only."
                : "Answers everything people post here."}
            </span>
          </span>
        </button>
        <div className="menu-sep" role="separator" />
        <button
          role="menuitem"
          className="status-option danger"
          onClick={() => {
            onClose(true);
            onRemove();
          }}
        >
          Remove from channel
        </button>
      </div>
    </>,
    document.body,
  );
}
