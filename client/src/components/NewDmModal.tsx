/* "New direct message" picker: people first, then the bots that take DMs.

   The people directory is the main feed's roster (every user), the same read
   AddMembersModal uses. Bots come from GET /bots, filtered to `dm_open`; the
   server's 404 for a closed bot is the wall, this list only avoids offering
   one. A bot DM answers every message, which the bot section says once. */

import { useEffect, useMemo, useState } from "react";

import { ApiError, listBots, listMembers } from "../api";
import { useChannels } from "../stores/channels";
import { useSession } from "../stores/session";
import type { Bot, ChannelMemberOut } from "../types";
import { Avatar, BotAvatar } from "./Avatar";

export interface NewDmModalProps {
  /** Called with the DM's channel id once it is open. */
  onOpened: (channelId: number) => void;
  onClose: () => void;
}

export function NewDmModal({ onOpened, onClose }: NewDmModalProps) {
  const mainFeedId = useChannels((s) => s.mainFeedId());
  const me = useSession((s) => s.user);
  const [people, setPeople] = useState<ChannelMemberOut[] | null>(null);
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let alive = true;
    const fail = (err: unknown) => {
      if (alive) {
        setError(err instanceof ApiError ? err.detail : "Could not load the directory");
      }
    };
    if (mainFeedId !== null) {
      listMembers(mainFeedId).then((rows) => {
        if (alive) setPeople(rows.filter((m) => m.type === "user"));
      }, fail);
    } else {
      setPeople([]);
    }
    listBots().then((rows) => {
      if (alive) setBots(rows);
    }, fail);
    return () => {
      alive = false;
    };
  }, [mainFeedId]);

  const { personRows, botRows } = useMemo(
    () => ({
      personRows: (people ?? [])
        .filter((p) => me === null || p.id !== me.id)
        .sort((a, b) => a.name.localeCompare(b.name)),
      botRows: (bots ?? [])
        .filter((b) => b.dm_open === true)
        .sort((a, b) => a.name.localeCompare(b.name)),
    }),
    [people, bots, me],
  );

  const open = (start: () => Promise<number>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    start().then(
      (channelId) => {
        setBusy(false);
        onOpened(channelId);
        onClose();
      },
      (err: unknown) => {
        setBusy(false);
        setError(err instanceof ApiError ? err.detail : "Could not open the DM");
      },
    );
  };

  const loading = people === null || bots === null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="bot-modal"
        role="dialog"
        aria-label="New direct message"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="bot-modal-head">
          <span className="bot-modal-title">New direct message</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="bot-modal-body">
          {loading && error === null && <p className="bot-modal-note">Loading…</p>}
          {!loading && personRows.length === 0 && botRows.length === 0 && (
            <p className="bot-modal-note">Nobody else to message yet.</p>
          )}
          {personRows.length > 0 && <div className="member-section">People</div>}
          {personRows.map((p) => (
            <button
              key={`u${p.id}`}
              className="bot-pick"
              disabled={busy}
              onClick={() => open(() => useChannels.getState().openDm(p.id))}
            >
              <Avatar src={p.avatar_url} name={p.name} />
              <span className="bot-pick-name">{p.name}</span>
            </button>
          ))}
          {botRows.length > 0 && (
            <>
              <div className="member-section">Bots</div>
              <p className="bot-modal-note">
                A bot answers every message in its DM.
              </p>
            </>
          )}
          {botRows.map((b) => (
            <button
              key={`b${b.id}`}
              className="bot-pick"
              disabled={busy}
              onClick={() => open(() => useChannels.getState().openBotDm(b.id))}
            >
              <BotAvatar src={b.avatar_url} name={b.name} />
              <span className="bot-pick-name">{b.name}</span>
              <span className="bot-tag">BOT</span>
            </button>
          ))}
        </div>
        {error !== null && <p className="bot-modal-error">{error}</p>}
      </div>
    </div>
  );
}
