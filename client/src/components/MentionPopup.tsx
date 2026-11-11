import { useLayoutEffect, useRef } from "react";

import type { MentionCandidate } from "../lib/mentionComplete";
import { Avatar, BotAvatar } from "./Avatar";

export const mentionOptionId = (listId: string, index: number) => `${listId}-${index}`;

interface MentionPopupProps {
  id: string;
  items: MentionCandidate[];
  activeIndex: number;
  onActive: (index: number) => void;
  onPick: (item: MentionCandidate) => void;
  /** A pointer is down on the list, so a blur of the textarea is not a leave. */
  onPress: (down: boolean) => void;
}

/* Nothing in here takes focus: the textarea keeps it, and the keys are the
   composer's. */
export function MentionPopup({
  id,
  items,
  activeIndex,
  onActive,
  onPick,
  onPress,
}: MentionPopupProps) {
  const listRef = useRef<HTMLDivElement | null>(null);

  /* scrollIntoView would also scroll the page behind an on-screen keyboard. */
  useLayoutEffect(() => {
    const list = listRef.current;
    const row = list?.children[activeIndex] as HTMLElement | undefined;
    if (list == null || row === undefined) return;
    const bottom = row.offsetTop + row.offsetHeight;
    if (row.offsetTop < list.scrollTop) list.scrollTop = row.offsetTop;
    else if (bottom > list.scrollTop + list.clientHeight) {
      list.scrollTop = bottom - list.clientHeight;
    }
  }, [activeIndex, items]);

  return (
    <div
      ref={listRef}
      id={id}
      className="mention-popup"
      role="listbox"
      aria-label="Mention someone"
      onMouseDown={(e) => e.preventDefault()}
      onPointerDown={() => onPress(true)}
      onPointerUp={() => onPress(false)}
      onPointerCancel={() => onPress(false)}
      onPointerLeave={() => onPress(false)}
    >
      {items.map((item, i) => (
        <div
          key={item.key}
          id={mentionOptionId(id, i)}
          className={`mention-option${i === activeIndex ? " active" : ""}`}
          role="option"
          aria-selected={i === activeIndex}
          onMouseMove={() => {
            if (i !== activeIndex) onActive(i);
          }}
          onClick={() => onPick(item)}
        >
          {item.kind === "user" ? (
            <Avatar src={item.avatarUrl} name={item.name} size={24} />
          ) : item.kind === "bot" ? (
            <BotAvatar src={item.avatarUrl} name={item.name} size={24} />
          ) : (
            <div className="avatar avatar-bot mention-group" aria-hidden>
              @
            </div>
          )}
          <span className="mention-name">
            {item.kind === "group" ? `@${item.name}` : item.name}
          </span>
          {item.hint !== "" && <span className="mention-hint">{item.hint}</span>}
        </div>
      ))}
    </div>
  );
}
