import { useState } from "react";

import { avatarUrl } from "../api";

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
