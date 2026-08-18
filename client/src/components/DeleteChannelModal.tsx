/* "Delete #channel" — the confirm dialog, Discord's shape.

   Deleting a channel takes its history with it, and nothing brings it back,
   so the button is not armed until the name has been typed out in full. That
   friction IS the feature: it makes the destructive click a deliberate one,
   and it is the same bar Discord sets.

   Only the owner and admins are offered this (the server 403s everyone else,
   and 400s the main feed and DMs, which are not deletable at all) — but a
   refusal is shown verbatim rather than swallowed, because the server, not
   this component, is the wall. */

import { useEffect, useState } from "react";

import { ApiError } from "../api";
import { useChannelDelete } from "../stores/channelDelete";
import { ChannelLabel } from "./LockGlyph";

export function DeleteChannelModal({
  channelId,
  channelName,
  isPrivate,
  onClose,
}: {
  channelId: number;
  /** Bare channel name, no "#" — this renders the 🔒/# itself. */
  channelName: string;
  isPrivate: boolean;
  onClose: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const confirmed = typed.trim() === channelName;
  const label = <ChannelLabel name={channelName} isPrivate={isPrivate} />;

  const submit = async () => {
    if (!confirmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      // The store tears the channel down on success — including navigating
      // off it, which unmounts this dialog — and stays idempotent with the
      // channel_delete frame that follows.
      await useChannelDelete.getState().remove(channelId);
    } catch (err) {
      setBusy(false);
      setError(
        err instanceof ApiError ? err.detail : "Failed to delete the channel",
      );
      return;
    }
    onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="delete-channel-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Delete ${channelName}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head">
          <span className="member-modal-title">Delete {label}</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>

        <p className="member-modal-note">
          Are you sure you want to delete {label}? This will permanently delete
          the channel and all of its messages. This cannot be undone.
        </p>

        <div className="field">
          <label htmlFor="delete-channel-confirm">
            Type the channel name to confirm
          </label>
          <input
            id="delete-channel-confirm"
            autoFocus
            autoComplete="off"
            spellCheck={false}
            /* The match is exact, and channel names are lowercase by rule —
               so a phone keyboard's opening capital would be an unwinnable
               dead end. */
            autoCapitalize="none"
            value={typed}
            maxLength={64}
            placeholder={channelName}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
          />
        </div>

        {error !== null && <p className="form-error">{error}</p>}

        <div className="member-modal-actions">
          <button className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn btn-danger"
            onClick={() => void submit()}
            disabled={busy || !confirmed}
          >
            {busy ? "Deleting…" : "Delete channel"}
          </button>
        </div>
      </div>
    </div>
  );
}
