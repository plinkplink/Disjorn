import { useState } from "react";

import { avatarUrl, botAvatarUrl } from "../api";

interface AvatarProps {
  userId: number;
  name: string;
  size?: number;
}

/** User avatar with initial-letter fallback (the server 404s when none set). */
export function Avatar({ userId, name, size = 32 }: AvatarProps) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="avatar" style={{ width: size, height: size }} aria-hidden>
      {name.slice(0, 1).toUpperCase()}
      {!failed && (
        <img
          src={avatarUrl(userId)}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
}

interface BotAvatarProps {
  botId: number;
  name: string;
  size?: number;
  /**
   * False when we positively know the bot has no avatar (message payloads
   * carry `author.avatar_path`), so no doomed request is made. Rosters don't
   * carry it — leave it true and let the 404 fall back.
   */
  hasAvatar?: boolean;
}

/**
 * Bot avatar with the same letter-tile fallback as humans. Bots keep their own
 * id space (a bot id can collide with a user id), hence a separate endpoint —
 * `/bots/{id}/avatar`, which 404s when none is set. Most bots have none, so the
 * letter tile is the normal case, not the error case: it is always rendered and
 * the image simply layers over it when it loads.
 */
export function BotAvatar({
  botId,
  name,
  size = 32,
  hasAvatar = true,
}: BotAvatarProps) {
  const [failed, setFailed] = useState(false);
  return (
    <div
      className="avatar avatar-bot"
      style={{ width: size, height: size }}
      aria-hidden
    >
      {name.slice(0, 1).toUpperCase()}
      {hasAvatar && !failed && (
        <img
          src={botAvatarUrl(botId)}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
}
