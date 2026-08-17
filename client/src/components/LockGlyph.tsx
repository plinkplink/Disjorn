/* The private-channel glyph — Discord's 🔒 in the slot the "#" occupies.

   Inline SVG rather than the emoji: it inherits currentColor and the sidebar's
   own muted greys, so a private row reads as the same kind of thing as a
   public one instead of sprouting a colour-emoji padlock that renders
   differently on every platform. Sized by CSS (.lock-glyph), like .hash. */

export function LockGlyph({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`lock-glyph ${className}`.trim()}
      viewBox="0 0 12 12"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M3.9 5.3V3.7a2.1 2.1 0 0 1 4.2 0v1.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
      <rect x="2.3" y="5.2" width="7.4" height="5.4" rx="1.3" fill="currentColor" />
    </svg>
  );
}

/** "🔒#name" / "#name" as one inline label — used in headers, modals, menus. */
export function ChannelLabel({
  name,
  isPrivate,
  prefix = "#",
}: {
  name: string;
  isPrivate: boolean;
  prefix?: string;
}) {
  return (
    <span className="channel-label">
      {isPrivate ? <LockGlyph /> : <span className="hash">{prefix}</span>}
      {name}
    </span>
  );
}
