/* Mention autocomplete for the composer. Pure: which `@token` the caret is in,
   who can be offered for it in this channel, and what accepting one writes. */

import type { ChannelMemberOut } from "../types";

export const GROUP_NAME = "bots";

/* The server's default GROUP_MENTION_SKIP_BOT_NAMES, which no payload carries:
   a house that overrides the setting shows a wrong hint and count here, and
   the server still decides who is called. */
export const SILENT_BOT_NAMES: readonly string[] = ["system", "broker", "BuildGable"];
/* Accounts nobody addresses: silent, and left off the list entirely. */
export const UNLISTED_BOT_NAMES: readonly string[] = ["system", "broker"];

export interface MentionToken {
  /** Index of the `@`. */
  start: number;
  /** The caret: the token is replaced up to here and no further. */
  end: number;
  query: string;
}

export interface MentionCandidate {
  key: string;
  kind: "user" | "bot" | "group";
  /** Written after the `@` exactly as stored. */
  name: string;
  avatarUrl: string | null;
  hint: string;
}

const OPENERS = new Set(["(", "[", "{", '"', "'", "*", "~", ">"]);

/* The server's two code patterns, plus code still being typed: an open fence,
   or an unclosed backtick earlier on the `@`'s own line. */
function inCode(text: string, at: number): boolean {
  let i = 0;
  while (i < at) {
    if (text.startsWith("```", i)) {
      const close = text.indexOf("```", i + 3);
      if (close === -1 || close > at) return true;
      i = close + 3;
    } else if (text[i] === "`") {
      let j = i + 1;
      while (j < text.length && text[j] !== "`" && text[j] !== "\n") j++;
      if (text[j] === "`" && j > i + 1) {
        if (j > at) return true;
        i = j + 1;
      } else {
        if (!text.slice(i, at).includes("\n")) return true;
        i += 1;
      }
    } else {
      i += 1;
    }
  }
  return false;
}

/** The `@token` the caret sits at the end of, or null when a mention is not
    being typed there. */
export function activeMention(
  text: string,
  selectionStart: number,
  selectionEnd: number = selectionStart,
): MentionToken | null {
  if (selectionStart !== selectionEnd || selectionStart > text.length) return null;
  let at = -1;
  for (let i = selectionStart - 1; i >= 0; i--) {
    const ch = text[i] as string;
    if (/\s/.test(ch)) return null;
    if (ch === "@") {
      at = i;
      break;
    }
  }
  if (at === -1) return null;
  const prev = at === 0 ? "" : (text[at - 1] as string);
  if (prev !== "" && !/\s/.test(prev) && !OPENERS.has(prev)) return null;
  if (inCode(text, at)) return null;
  return { start: at, end: selectionStart, query: text.slice(at + 1, selectionStart) };
}

export interface CandidateOptions {
  /** The reader's own user id: they are listed last among the people. */
  selfUserId: number | null;
  /** The server addresses every message here to the channel's bot (a build chat). */
  everyMessage?: boolean;
  /** The text is an edit, and an edit calls nobody. */
  editing?: boolean;
  silentBotNames?: readonly string[];
}

/** Everyone who can be mentioned in a channel, in the order an empty `@`
    lists them: answering bots, `@bots`, people, then a bot that never answers. */
export function mentionCandidates(
  members: readonly ChannelMemberOut[],
  opts: CandidateOptions,
): MentionCandidate[] {
  const silent = new Set(
    (opts.silentBotNames ?? SILENT_BOT_NAMES).map((n) => n.toLowerCase()),
  );
  const answering: MentionCandidate[] = [];
  const quiet: MentionCandidate[] = [];
  const people: MentionCandidate[] = [];
  let self: MentionCandidate | null = null;
  for (const m of members) {
    const base = { key: `${m.type}:${m.id}`, name: m.name, avatarUrl: m.avatar_url ?? null };
    if (m.type === "user") {
      if (m.id === opts.selfUserId) self = { ...base, kind: "user", hint: "you" };
      else people.push({ ...base, kind: "user", hint: "" });
    } else if (UNLISTED_BOT_NAMES.includes(m.name.toLowerCase())) {
      continue;
    } else if (silent.has(m.name.toLowerCase())) {
      quiet.push({ ...base, kind: "bot", hint: "does not reply" });
    } else {
      const always = opts.everyMessage === true || m.reply_mode === "always";
      answering.push({
        ...base,
        kind: "bot",
        hint:
          opts.editing === true
            ? "not called by an edit"
            : always
              ? "replies to everything here"
              : "replies when mentioned",
      });
    }
  }
  const group: MentionCandidate[] =
    answering.length === 0
      ? []
      : [
          {
            key: `group:${GROUP_NAME}`,
            kind: "group",
            name: GROUP_NAME,
            avatarUrl: null,
            hint:
              opts.editing === true
                ? "not called by an edit"
                : `every bot in this room (${answering.length})`,
          },
        ];
  return [...answering, ...group, ...people, ...(self === null ? [] : [self]), ...quiet];
}

/** Names that start with the query, then names that contain it; each group
    keeps the candidates' own order. Case-insensitive. Empty means no match. */
export function rankMentions(
  candidates: readonly MentionCandidate[],
  query: string,
): MentionCandidate[] {
  const q = query.toLowerCase();
  const starts: MentionCandidate[] = [];
  const contains: MentionCandidate[] = [];
  for (const c of candidates) {
    const at = c.name.toLowerCase().indexOf(q);
    if (at === 0) starts.push(c);
    else if (at > 0) contains.push(c);
  }
  return [...starts, ...contains];
}

/** Replace the token with `@name ` and say where the caret goes. Text after
    the caret is kept, and a space already there is stepped over, not doubled. */
export function acceptMention(
  text: string,
  token: MentionToken,
  name: string,
): { text: string; caret: number } {
  const head = `${text.slice(0, token.start)}@${name}`;
  const tail = text.slice(token.end);
  if (tail.startsWith(" ")) return { text: head + tail, caret: head.length + 1 };
  return { text: `${head} ${tail}`, caret: head.length + 1 };
}

export interface MentionState {
  token: MentionToken;
  /** The highlighted row among the ranked matches. */
  index: number;
  /** Dismissed with Escape: closed until the caret leaves this token. */
  off: boolean;
}

/** The popup's state after the text or the caret changed. Only typing starts
    a token, so a caret that lands after an old `@name` opens nothing. */
export function trackMention(
  prev: MentionState | null,
  token: MentionToken | null,
  typed: boolean,
): MentionState | null {
  if (token === null) return null;
  if (prev !== null && prev.token.start === token.start) {
    return {
      token,
      index: prev.token.query === token.query ? prev.index : 0,
      off: prev.off,
    };
  }
  return typed ? { token, index: 0, off: false } : null;
}

export type MentionKeyAction = "down" | "up" | "accept" | "dismiss" | "compose";

export interface KeyFacts {
  key: string;
  shiftKey: boolean;
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  isComposing: boolean;
  keyCode: number;
}

/** What a key does while the popup is open; null leaves it to the composer.
    `compose` is an IME's own key: it neither accepts a row nor sends. */
export function mentionKey(e: KeyFacts): MentionKeyAction | null {
  if (e.isComposing || e.keyCode === 229) return "compose";
  if (e.key === "Escape") return "dismiss";
  if ((e.key === "Enter" || e.key === "Tab") && !e.shiftKey) return "accept";
  if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return null;
  if (e.key === "ArrowDown") return "down";
  if (e.key === "ArrowUp") return "up";
  return null;
}
