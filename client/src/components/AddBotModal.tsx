/* "Add a bot to this channel" picker (closes the DEFERRED item "No UI for DM
   bot membership": POST /channels/{id}/bots existed but only the API/SDK could
   reach it).

   Two steps on purpose. A bot in a channel is a reader of that channel — it
   receives the live stream AND can backfill the history — and for a DM that is
   the whole privacy question, so the consequence is spelled out on screen at
   the moment of the decision rather than hidden in a tooltip.

   The wall itself is server-side: POST /channels/{id}/bots is participant-gated
   (channels.py `_require_bot_manage_access`). This component never claims a
   permission it lacks — UserPanel only mounts it when the viewer is in the
   channel's own member roster — and a refusal is surfaced verbatim rather than
   swallowed. Esc or backdrop click closes. */

import { useEffect, useState } from "react";

import { addChannelBot, ApiError, listBots } from "../api";
import type { Bot } from "../types";
import { BotAvatar } from "./Avatar";

/** The seeded message-author row; it can't authenticate, so it isn't joinable. */
const SYSTEM_BOT_NAME = "system";

export interface AddBotModalProps {
  channelId: number;
  /** Display label for the channel ("#main" / "@alice"). */
  channelLabel: string;
  isDm: boolean;
  /** Bots already in the channel — hidden from the picker. */
  existingBotIds: number[];
  /** Called after a successful add so the caller can refresh its roster. */
  onAdded: () => void;
  onClose: () => void;
}

export function AddBotModal({
  channelId,
  channelLabel,
  isDm,
  existingBotIds,
  onAdded,
  onClose,
}: AddBotModalProps) {
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Bot | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    listBots().then(
      (all) => setBots(all),
      (err: unknown) =>
        setError(err instanceof ApiError ? err.detail : "Failed to load bots"),
    );
  }, []);

  const candidates = (bots ?? []).filter(
    (b) => b.name !== SYSTEM_BOT_NAME && !existingBotIds.includes(b.id),
  );

  const confirmAdd = () => {
    if (chosen === null || busy) return;
    setBusy(true);
    setError(null);
    addChannelBot(channelId, chosen.id).then(
      () => {
        setBusy(false);
        onAdded();
        onClose();
      },
      (err: unknown) => {
        setBusy(false);
        // Includes the server's 403 when the viewer is not a participant —
        // shown as-is instead of pretending the add worked.
        setError(err instanceof ApiError ? err.detail : "Failed to add the bot");
      },
    );
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="bot-modal"
        role="dialog"
        aria-label={`Add a bot to ${channelLabel}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="bot-modal-head">
          <span className="bot-modal-title">Add a bot to {channelLabel}</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>

        {chosen === null ? (
          <div className="bot-modal-body">
            {bots === null && error === null && (
              <p className="bot-modal-note">Loading bots…</p>
            )}
            {bots !== null && candidates.length === 0 && (
              <p className="bot-modal-note">
                No other bots to add — every bot is already here.
              </p>
            )}
            {candidates.map((b) => (
              <button
                key={b.id}
                className="bot-pick"
                onClick={() => {
                  setError(null);
                  setChosen(b);
                }}
              >
                <BotAvatar src={b.avatar_url} name={b.name} />
                <span className="bot-pick-name">{b.name}</span>
                <span className="bot-tag">BOT</span>
              </button>
            ))}
          </div>
        ) : (
          <div className="bot-modal-body">
            <div className="bot-confirm-who">
              <BotAvatar src={chosen.avatar_url} name={chosen.name} size={38} />
              <span className="bot-pick-name">{chosen.name}</span>
            </div>
            <p className="bot-consequence">
              {isDm ? (
                <>
                  <strong>{chosen.name}</strong> will be able to read this
                  private conversation — every message either of you sends from
                  now on, and the messages already in it. There is no way to
                  add it for one side only.
                </>
              ) : (
                <>
                  <strong>{chosen.name}</strong> will receive every message
                  posted in {channelLabel}, including the ones already there.
                </>
              )}
            </p>
            <p className="bot-modal-note">
              You can remove it again from the member list, but anything it has
              already read stays read.
            </p>
          </div>
        )}

        {error !== null && <p className="bot-modal-error">{error}</p>}

        {chosen !== null && (
          <div className="bot-modal-actions">
            <button className="btn" onClick={() => setChosen(null)} disabled={busy}>
              Back
            </button>
            <button className="btn btn-primary" onClick={confirmAdd} disabled={busy}>
              {busy ? "Adding…" : `Add ${chosen.name}`}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
