/* "#channel was deleted" — the corner notice for someone who happened to be
   reading a channel at the moment it went away.

   Not a modal: nothing was done TO this person and there is nothing to
   decide, so it blocks nothing and clears itself. It is only ever raised for
   a viewer who was looking at the channel — everyone else just watches the
   row disappear, which is Discord's behaviour and needs no words. */

import { useEffect } from "react";

import { useChannelDelete } from "../stores/channelDelete";
import { ChannelLabel } from "./LockGlyph";

const DISMISS_MS = 8000;

export function ChannelDeletedToast() {
  const notice = useChannelDelete((s) => s.notice);
  const dismiss = useChannelDelete((s) => s.dismissNotice);

  useEffect(() => {
    if (notice === null) return;
    const timer = setTimeout(dismiss, DISMISS_MS);
    return () => clearTimeout(timer);
  }, [notice, dismiss]);

  if (notice === null) return null;
  return (
    <div className="channel-toast" role="status" aria-live="polite">
      <span className="channel-toast-text">
        <ChannelLabel name={notice.channelName} isPrivate={notice.isPrivate} />{" "}
        was deleted.
      </span>
      <button
        className="icon-btn"
        aria-label="Dismiss notice"
        onClick={dismiss}
      >
        ✕
      </button>
    </div>
  );
}
