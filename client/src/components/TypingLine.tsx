/* The typing line (WP10), lifted out of ChatView so the build modal can show
   the same one (stage 3, docket item 4).

   It is the SAME component, not a copy: the build session's channel is a real
   channel with real presence, and a second implementation would be a second
   answer to "is the builder typing" the first time one of them drifted. */

import { useMembers } from "../stores/members";
import { usePresence } from "../stores/presence";
import { useSession } from "../stores/session";

export function TypingLine({ channelId }: { channelId: number }) {
  const typists = usePresence((s) => s.typing[channelId]);
  const me = useSession((s) => s.user);
  const members = useMembers((s) => s.byChannel[channelId]);

  const others = (typists ?? []).filter(
    (t) => !(t.authorType === "user" && me !== null && t.authorId === me.id),
  );

  let text = "";
  if (others.length > 0) {
    const names = others.map(
      (t) =>
        members?.find((m) => m.type === t.authorType && m.id === t.authorId)
          ?.name ?? "Someone",
    );
    if (names.length === 1) text = `${names[0] ?? "Someone"} is typing`;
    else if (names.length === 2) text = `${names[0]} and ${names[1]} are typing`;
    else text = "Several people are typing";
  }

  // Fixed-height line: reserves space so the feed doesn't jump.
  return (
    <div className="typing-line" aria-live="polite">
      {text.length > 0 && (
        <>
          <span className="typing-dots" aria-hidden>
            <i />
            <i />
            <i />
          </span>
          {text}…
        </>
      )}
    </div>
  );
}
