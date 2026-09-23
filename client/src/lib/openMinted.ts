/* Opening a minted app URL in a new tab, without losing the user's click.
 *
 * Every Open button in the client does the same two things: ask the house for
 * a grant (`POST /apps/{id}/open`) and put the URL it mints into a new tab.
 * The obvious spelling — `window.open(url)` inside the promise's `then` — is
 * the one that silently does nothing. A browser lets a page open a tab only
 * while it is still inside the task the user's click started; by the time the
 * mint lands that task is long over, so Safari, Firefox and installed PWAs
 * hand back `null` and no tab appears. Nothing throws. The button just looks
 * broken (Claudette #2555).
 *
 * So the tab is opened SYNCHRONOUSLY on the click — blank, before the request
 * is even in flight — and pointed at the URL when the mint lands. The grant is
 * short-lived and single-use-ish by design, which is why the blank tab waits
 * for the URL rather than the URL waiting for a tab.
 *
 * Three call sites use this and none of them spells it out for itself: the
 * channel card, the build modal's Open, and the read-only card in the shell.
 * A behaviour that lives in three places drifts in two of them.
 */

/**
 * The blank tab, or null when the browser refused to give us one.
 *
 * `noopener` is NOT passed to `window.open` here, and that is deliberate: with
 * it, the spec says `window.open` returns null — the whole point of the flag
 * is that the opener gets no handle — and a null handle is exactly what this
 * pattern needs to keep. The tie is cut the other way instead, by clearing
 * `opener` on the child before it is navigated anywhere, which leaves the new
 * tab with no reference back to this window by the time it holds the app.
 */
function openBlankTab(): Window | null {
  let child: Window | null = null;
  try {
    child = window.open("about:blank", "_blank");
  } catch {
    return null;
  }
  if (child === null) return null;
  try {
    // Same effect as `noopener`, applied where we can still see the handle.
    child.opener = null;
  } catch {
    /* A browser that refuses the assignment still gets the tab; it just keeps
       the opener link. Worth a tab, not worth an aborted open. */
  }
  try {
    // A blank tab for the half-second the mint takes reads as a hung browser.
    child.document.write(
      "<!doctype html><meta charset=utf-8><title>Opening…</title>" +
        "<body style=\"font:14px system-ui,sans-serif;padding:2rem;color:#555\">" +
        "Opening…",
    );
    child.document.close();
  } catch {
    /* Cosmetic only. */
  }
  return child;
}

/**
 * What happened, for the button to say.
 *
 * `blocked` carries the URL because there is still a good answer in that case:
 * render it as a link the user can click. That click is a user gesture of its
 * own, so it opens where our programmatic one could not.
 */
export type MintedOpen =
  | { kind: "opened" }
  | { kind: "blocked"; url: string }
  | { kind: "failed"; error: unknown };

/** One sentence for the `blocked` case. Says what the browser did, not what
    the user did wrong, and it is the same sentence in all three places. */
export const POPUP_BLOCKED_NOTE =
  "Your browser blocked the new tab — this link opens it:";

/**
 * Open the URL `mint` resolves to, in a tab claimed on the current click.
 *
 * MUST be called synchronously from the click handler: the tab is claimed on
 * the way in, before `mint()` is awaited. Never resolves to a rejection — the
 * caller reads `kind` and says the right thing beside its own button.
 *
 * - tab granted, mint succeeded → the tab navigates; `opened`
 * - tab granted, mint failed    → the tab is CLOSED (an orphan blank tab is
 *   worse than no tab) and the error comes back for the caller to phrase
 * - tab refused (popup blocker) → `blocked` with the minted URL
 * - tab refused AND mint failed → `failed`, since there is no URL to offer
 */
export function openMinted(mint: () => Promise<string>): Promise<MintedOpen> {
  const child = openBlankTab();
  return mint().then(
    (url): MintedOpen => {
      if (child === null) return { kind: "blocked", url };
      try {
        child.location.href = url;
      } catch {
        // Handle went stale (the user closed it while we waited). The URL is
        // still good, so offer it the same way a blocked popup is offered.
        return { kind: "blocked", url };
      }
      return { kind: "opened" };
    },
    (error: unknown): MintedOpen => {
      if (child !== null) {
        try {
          child.close();
        } catch {
          /* Already gone. */
        }
      }
      return { kind: "failed", error };
    },
  );
}
