/* "Add members" picker for a private channel — the second step of creating one,
   and the owner's way to invite later (member panel / channel menu).

   Only the owner can invite (RULED 2026-08-12; the server 403s everyone else),
   so this is only ever mounted for the owner — but a refusal is still shown
   verbatim rather than swallowed, because the server, not this component, is
   the wall.

   The user directory is the main feed's roster: main_feed is public and lists
   every user, and it is the only "all users" read this client has. Bots are
   listed from GET /bots and added through the existing bot-membership
   endpoint, which enforces the same owner-only rule on a private channel. */

import { useEffect, useMemo, useState } from "react";

import {
  addChannelBot,
  ApiError,
  inviteToChannel,
  listBots,
  listMembers,
} from "../api";
import { useChannels } from "../stores/channels";
import type { Bot, ChannelMemberOut } from "../types";
import { Avatar, BotAvatar } from "./Avatar";
import { LockGlyph } from "./LockGlyph";

/** The seeded message-author row; it can't authenticate, so it isn't joinable. */
const SYSTEM_BOT_NAME = "system";

export interface AddMembersModalProps {
  channelId: number;
  /** Bare channel name, no "#" — this renders the 🔒# itself. */
  channelName: string;
  /** Label for the close button ("Skip" right after creating, else "Cancel"). */
  closeLabel?: string;
  /** Called after at least one successful add, so the caller can refresh. */
  onAdded: () => void;
  onClose: () => void;
}

export function AddMembersModal({
  channelId,
  channelName,
  closeLabel = "Cancel",
  onAdded,
  onClose,
}: AddMembersModalProps) {
  const mainFeedId = useChannels((s) => s.mainFeedId());
  const [directory, setDirectory] = useState<ChannelMemberOut[] | null>(null);
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [current, setCurrent] = useState<ChannelMemberOut[] | null>(null);
  const [pickedUsers, setPickedUsers] = useState<number[]>([]);
  const [pickedBots, setPickedBots] = useState<number[]>([]);
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
        setError(err instanceof ApiError ? err.detail : "Could not load people");
      }
    };
    // The channel's own roster: we are its owner, so this read is ours to make.
    listMembers(channelId).then((rows) => {
      if (alive) setCurrent(rows);
    }, fail);
    if (mainFeedId !== null) {
      listMembers(mainFeedId).then((rows) => {
        if (alive) setDirectory(rows.filter((m) => m.type === "user"));
      }, fail);
    } else {
      setDirectory([]);
    }
    listBots().then((rows) => {
      if (alive) setBots(rows);
    }, fail);
    return () => {
      alive = false;
    };
  }, [channelId, mainFeedId]);

  const { userCandidates, botCandidates } = useMemo(() => {
    const inChannelUsers = new Set(
      (current ?? []).filter((m) => m.type === "user").map((m) => m.id),
    );
    const inChannelBots = new Set(
      (current ?? []).filter((m) => m.type === "bot").map((m) => m.id),
    );
    return {
      userCandidates: (directory ?? [])
        .filter((u) => !inChannelUsers.has(u.id))
        .sort((a, b) => a.name.localeCompare(b.name)),
      botCandidates: (bots ?? [])
        .filter((b) => b.name !== SYSTEM_BOT_NAME && !inChannelBots.has(b.id))
        .sort((a, b) => a.name.localeCompare(b.name)),
    };
  }, [current, directory, bots]);

  const loading = current === null || directory === null || bots === null;
  const chosen = pickedUsers.length + pickedBots.length;

  const toggle = (
    id: number,
    picked: number[],
    setPicked: (next: number[]) => void,
  ) => {
    setPicked(
      picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id],
    );
  };

  const submit = async () => {
    if (chosen === 0 || busy) return;
    setBusy(true);
    setError(null);
    let added = false;
    try {
      // One call per pick — /invite and /channels/{id}/bots each take one
      // subject, and a failure part-way still leaves the successful adds in
      // place (the roster refetch below shows exactly who made it in).
      for (const userId of pickedUsers) {
        await inviteToChannel(channelId, userId);
        added = true;
      }
      for (const botId of pickedBots) {
        await addChannelBot(channelId, botId);
        added = true;
      }
    } catch (err) {
      setBusy(false);
      setError(err instanceof ApiError ? err.detail : "Could not add everyone");
      if (added) onAdded();
      listMembers(channelId).then(setCurrent, () => {
        /* keep the stale roster; the error above is the message that matters */
      });
      setPickedUsers([]);
      setPickedBots([]);
      return;
    }
    setBusy(false);
    if (added) onAdded();
    onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="member-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Add members to ${channelName}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="member-modal-head">
          <span className="member-modal-title">
            Add members to <LockGlyph />
            {channelName}
          </span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>

        <p className="member-modal-note">
          Only members can read this channel — including its history. Anyone you
          add can read everything already posted here.
        </p>

        <div className="member-modal-body">
          {loading && error === null && (
            <p className="member-modal-note">Loading…</p>
          )}
          {!loading && userCandidates.length === 0 && (
            <p className="member-modal-note">Everyone is already in here.</p>
          )}
          {userCandidates.map((u) => (
            <label className="member-pick" key={`u${u.id}`}>
              <input
                type="checkbox"
                checked={pickedUsers.includes(u.id)}
                onChange={() => toggle(u.id, pickedUsers, setPickedUsers)}
              />
              <Avatar src={u.avatar_url} name={u.name} />
              <span className="member-pick-name">{u.name}</span>
            </label>
          ))}

          {botCandidates.length > 0 && (
            <div className="member-modal-section">Bots</div>
          )}
          {botCandidates.map((b) => (
            <label className="member-pick" key={`b${b.id}`}>
              <input
                type="checkbox"
                checked={pickedBots.includes(b.id)}
                onChange={() => toggle(b.id, pickedBots, setPickedBots)}
              />
              <BotAvatar src={b.avatar_url} name={b.name} />
              <span className="member-pick-name">{b.name}</span>
              <span className="bot-tag">BOT</span>
            </label>
          ))}
          {botCandidates.length > 0 && (
            <p className="member-modal-note">
              A bot you add reads this channel exactly as a person does — the
              live stream and the history both.
            </p>
          )}
        </div>

        {error !== null && <p className="form-error">{error}</p>}

        <div className="member-modal-actions">
          <button className="btn" onClick={onClose} disabled={busy}>
            {closeLabel}
          </button>
          <button
            className="btn btn-primary"
            onClick={() => void submit()}
            disabled={busy || chosen === 0}
          >
            {busy ? "Adding…" : chosen === 0 ? "Add" : `Add ${chosen}`}
          </button>
        </div>
      </div>
    </div>
  );
}
