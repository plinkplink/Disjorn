/* The app card in a channel (SPECS/2026-09-09-apps-serving-gate.md, D8).
 *
 * There is no "app card" message type and no schema change. Share posts an
 * ORDINARY message — `shared an app: **<name>** — <origin>/<id>/` — and this
 * component is the client noticing that a message's first link is an app on
 * this house's gate and drawing the card instead of an unfurl. A client that
 * predates this file shows the same message as a link that works, which is
 * the whole reason the card is not a new frame on the wire.
 *
 * It never asks the gate for anything. The link is matched by STRING against
 * the origin the house told us about, and everything on the card comes from
 * the house's own `/apps/{id}/card`. A 404 there means "not entitled": the
 * card renders nothing at all and the plain link stands, because a link to
 * something you cannot see should look like a link, not like a locked door.
 */

import { useEffect, useState } from "react";

import { ApiError } from "../api";
import { useApps } from "../stores/apps";
import { BotAvatar } from "./Avatar";

/**
 * The app id in `url`, when `url` is a link to an app on THIS house's gate.
 *
 * Exact origin match, then a 12-character id (D4's random base32), then a
 * boundary — so `<origin>/<id>/` and `<origin>/<id>/page?x=1` both match and
 * `<origin>/<id>extra/` does not. An empty `originBase` (no gate configured)
 * matches nothing: there is no origin for a link to be on.
 *
 * Everything is a string comparison. Nothing here parses a URL loosely or
 * matches a suffix — a card is a claim that the house serves this app, and a
 * lax match would let any host that ends in ours make that claim.
 */
export function appIdFromUrl(
  url: string | null,
  originBase: string,
): string | null {
  if (url === null || originBase === "") return null;
  const base = originBase.replace(/\/+$/, "");
  if (!url.startsWith(`${base}/`)) return null;
  const rest = url.slice(base.length + 1);
  const match = /^([A-Za-z0-9]{12})(?:[/?#]|$)/.exec(rest);
  return match === null ? null : (match[1] ?? null);
}

/**
 * The tile's colour, hashed from the app id.
 *
 * A hash rather than a random or a palette index: the tile is the app's face
 * until a screenshot exists, and a face has to be the same one in every room
 * the app is shared into, on every viewer's screen.
 */
function tileStyle(appId: string): { background: string } {
  let hue = 0;
  for (const ch of appId) hue = (hue * 31 + ch.charCodeAt(0)) % 360;
  const to = (hue + 40) % 360;
  return {
    background: `linear-gradient(140deg, hsl(${hue} 52% 42%), hsl(${to} 52% 30%))`,
  };
}

/** The first letter of the name, uppercased — the whole picture, until a
    screenshot exists (D9 is deferred, and `image_url` is its seam). */
function initialOf(name: string): string {
  const trimmed = name.trim();
  return trimmed.length === 0 ? "?" : (trimmed[0] ?? "?").toUpperCase();
}

export function AppCard({ appId }: { appId: string }) {
  const entry = useApps((s) => s.cards[appId]);
  const [busy, setBusy] = useState<"open" | "remix" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void useApps.getState().loadCard(appId);
  }, [appId]);

  if (entry === undefined || entry.status !== "done" || entry.data === null) {
    return null;
  }
  const card = entry.data;

  const open = () => {
    if (busy !== null) return;
    setBusy("open");
    setError(null);
    useApps
      .getState()
      .openApp(card.id, "live")
      .then(
        (url) => {
          setBusy(null);
          window.open(url, "_blank", "noopener");
        },
        (err: unknown) => {
          setBusy(null);
          setError(err instanceof ApiError ? err.detail : "Could not open it");
        },
      );
  };

  const remix = () => {
    if (busy !== null) return;
    setBusy("remix");
    setError(null);
    useApps
      .getState()
      .remixApp(card.id)
      .then(
        (session) => {
          setBusy(null);
          // The shell owns the build modal; this hands it the session the
          // same way the chooser's `onStarted` does.
          useApps.getState().requestBuildModal(session.id);
        },
        (err: unknown) => {
          setBusy(null);
          setError(
            err instanceof ApiError ? err.detail : "Could not remix it",
          );
        },
      );
  };

  return (
    <div className="app-link-card">
      {card.image_url === null ? (
        <span className="app-link-tile" style={tileStyle(card.id)} aria-hidden>
          {initialOf(card.name)}
        </span>
      ) : (
        <img
          className="app-link-shot"
          src={card.image_url}
          alt=""
          loading="lazy"
        />
      )}
      <div className="app-link-text">
        <div className="app-link-head">
          <span className="app-link-name">{card.name}</span>
          <span className={`app-status-chip ${card.status}`}>{card.status}</span>
        </div>
        {card.description.length > 0 && (
          <p className="app-link-desc">{card.description}</p>
        )}
        <div className="app-link-meta">
          <BotAvatar
            src={card.builder.avatar_url}
            name={card.builder.name}
            size={20}
          />
          <span>
            built by {card.builder.name} for {card.owner.name}
          </span>
        </div>
        {error !== null && <p className="form-error">{error}</p>}
      </div>
      <div className="app-link-actions">
        <button
          className="btn"
          disabled={busy !== null || card.status !== "live"}
          title={
            card.status === "live"
              ? "Open this app in a new tab"
              : "This app is not live yet."
          }
          onClick={open}
        >
          {busy === "open" ? "Opening…" : "Open"}
        </button>
        {card.can_remix && (
          <button
            className="btn"
            disabled={busy !== null}
            title="Copy this app into one of your own and start building on it"
            onClick={remix}
          >
            {busy === "remix" ? "Remixing…" : "Remix"}
          </button>
        )}
      </div>
    </div>
  );
}
