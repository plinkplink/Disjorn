/* "You've been added to 🔒#room" / "You've been removed from 🔒#room".

   A modal, not a toast, for now (plink's call): being let into — or out of — a
   private room is not something to miss while scrolled away. A self-leave
   raises no notice at all; the membership store filters those out before they
   reach here.

   The actor's name comes from whatever roster this client has already loaded
   (the main feed's lists every user). When it doesn't resolve, the sentence
   says "someone" rather than inventing a name or leaking an id. */

import { useEffect } from "react";

import { useMembers } from "../stores/members";
import { useMembership } from "../stores/membership";
import { ChannelLabel } from "./LockGlyph";

export function MembershipNotice({
  onOpenChannel,
}: {
  onOpenChannel: (channelId: number) => void;
}) {
  const notice = useMembership((s) => s.notice);
  const dismiss = useMembership((s) => s.dismissNotice);
  const byName = useMembers((s) =>
    notice !== null && notice.byUserId !== null
      ? s.userName(notice.byUserId)
      : null,
  );

  useEffect(() => {
    if (notice === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [notice, dismiss]);

  if (notice === null) return null;

  const label = (
    <ChannelLabel
      name={notice.channelName}
      isPrivate={notice.visibility === "private"}
    />
  );
  const added = notice.kind === "added";

  return (
    <div className="modal-backdrop" onClick={dismiss}>
      <div
        className="membership-notice"
        role="dialog"
        aria-modal="true"
        aria-label={
          added
            ? `You have been added to ${notice.channelName}`
            : `You have been removed from ${notice.channelName}`
        }
        onClick={(e) => e.stopPropagation()}
      >
        <p className="membership-notice-title">
          {added ? <>You've been added to {label}</> : <>You've been removed from {label}</>}
        </p>
        <p className="membership-notice-body">
          {added ? (
            <>Added by {byName ?? "someone"}.</>
          ) : byName !== null ? (
            <>Removed by {byName}. You can no longer read it.</>
          ) : (
            <>You can no longer read it.</>
          )}
        </p>
        <div className="member-modal-actions">
          <button className="btn" onClick={dismiss}>
            Dismiss
          </button>
          {added && (
            <button
              className="btn btn-primary"
              onClick={() => {
                onOpenChannel(notice.channelId);
                dismiss();
              }}
            >
              Open
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
