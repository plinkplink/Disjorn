import { useState } from "react";

interface AvatarProps {
  /**
   * The payload's `avatar_url` (see types.ts): a versioned serving URL, or
   * null/undefined when this actor has no avatar. Null is the server telling
   * us not to ask — the letter tile stands alone and no doomed request goes
   * out. Every message author, roster row, bot and user payload now carries
   * it, so no caller has to guess a URL from an id.
   */
  src: string | null | undefined;
  name: string;
  size?: number;
}

/**
 * Letter tile with the avatar image layered over it. The tile is ALWAYS
 * rendered — no avatar is the normal case for most bots and for humans who
 * never set a picture, not an error state — and the <img> simply covers it
 * once it loads.
 *
 * `failedSrc` rather than a boolean so a src change re-arms the image: when an
 * avatar is re-uploaded the payload's `?v={mtime}` changes, and this component
 * must try the new URL even though the previous one failed.
 */
function AvatarTile({
  src,
  name,
  size,
  className,
}: AvatarProps & { size: number; className: string }) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  return (
    <div className={className} style={{ width: size, height: size }} aria-hidden>
      {name.slice(0, 1).toUpperCase()}
      {src != null && src !== failedSrc && (
        <img src={src} alt="" loading="lazy" onError={() => setFailedSrc(src)} />
      )}
    </div>
  );
}

/** User avatar with initial-letter fallback. */
export function Avatar({ src, name, size = 32 }: AvatarProps) {
  return <AvatarTile src={src} name={name} size={size} className="avatar" />;
}

/**
 * Bot avatar — same tile, own accent. Bots keep their own id space (a bot id
 * can collide with a user id) and so are served from a separate endpoint;
 * `avatar_url` already names the right one, which is why this no longer builds
 * a URL itself.
 */
export function BotAvatar({ src, name, size = 32 }: AvatarProps) {
  return (
    <AvatarTile src={src} name={name} size={size} className="avatar avatar-bot" />
  );
}
