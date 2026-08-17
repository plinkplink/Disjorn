/* "Create Channel" — name, public/private, then (for a private one) the
   people who get in.

   The window.prompt this replaced could only ever make a public channel. The
   toggle is the whole point: a private channel starts with exactly one member,
   its creator, and only its creator can add anyone else, so the "Add members"
   step follows creation rather than preceding it — the channel has to exist
   before anyone can be invited into it. Skip is a real answer: a private
   channel of one is a valid thing to have made. */

import { useEffect, useState } from "react";

import { ApiError, createChannel } from "../api";
import { useChannels } from "../stores/channels";
import type { ChannelListItem } from "../types";
import { AddMembersModal } from "./AddMembersModal";
import { LockGlyph } from "./LockGlyph";

/** Mirrors CHANNEL_NAME_RE in server/app/routers/channels.py. */
const NAME_RE = /^[a-z0-9-]{1,32}$/;

export function CreateChannelModal({
  onCreated,
  onClose,
}: {
  /** Called once the channel exists, so the caller can jump to it. */
  onCreated: (channel: ChannelListItem) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [isPrivate, setIsPrivate] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** Set once creation succeeded: the modal becomes the member picker. */
  const [created, setCreated] = useState<ChannelListItem | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const cleaned = name.trim().toLowerCase();
  const valid = NAME_RE.test(cleaned);

  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const channel = await createChannel(
        cleaned,
        isPrivate ? "private" : "public",
      );
      await useChannels.getState().refresh();
      onCreated(channel);
      if (isPrivate) {
        setBusy(false);
        setCreated(channel); // step 2: who else gets in
        return;
      }
      onClose();
    } catch (err) {
      setBusy(false);
      setError(
        err instanceof ApiError ? err.detail : "Failed to create the channel",
      );
    }
  };

  if (created !== null) {
    return (
      <AddMembersModal
        channelId={created.id}
        channelName={created.name ?? cleaned}
        closeLabel="Skip"
        onAdded={() => void useChannels.getState().refresh()}
        onClose={onClose}
      />
    );
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="create-channel-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Create channel"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head">
          <span className="member-modal-title">Create channel</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="field">
          <label htmlFor="create-channel-name">Channel name</label>
          <div className="create-channel-name">
            <span className="hash" aria-hidden>
              #
            </span>
            <input
              id="create-channel-name"
              autoFocus
              value={name}
              maxLength={32}
              placeholder="new-channel"
              onChange={(e) => setName(e.target.value.toLowerCase())}
              onKeyDown={(e) => {
                if (e.key === "Enter") void submit();
              }}
            />
          </div>
          <p className="field-hint">
            1–32 characters: lowercase letters, numbers and dashes.
          </p>
        </div>

        <label className="create-channel-private">
          <input
            type="checkbox"
            checked={isPrivate}
            onChange={(e) => setIsPrivate(e.target.checked)}
          />
          <span className="create-channel-private-text">
            <span className="create-channel-private-label">
              <LockGlyph />
              Private channel
            </span>
            <span className="field-hint">
              {isPrivate
                ? "Only the members you add can read this channel. You can invite people in the next step."
                : "Anyone on the server can read and post here."}
            </span>
          </span>
        </label>

        {error !== null && <p className="form-error">{error}</p>}

        <div className="member-modal-actions">
          <button className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            onClick={() => void submit()}
            disabled={busy || !valid}
          >
            {busy ? "Creating…" : "Create channel"}
          </button>
        </div>
      </div>
    </div>
  );
}
