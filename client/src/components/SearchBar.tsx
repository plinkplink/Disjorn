/* Topbar message search (WP12, spec §5.1). Debounced (300ms, min 2 chars)
   as-you-type, or Enter for an immediate search. Results render in an overlay
   dropdown anchored under the topbar (one design for every viewport — no
   separate desktop panel). Below 768px the input collapses to a 🔍 button
   that expands over the topbar row.

   Result click: activate the channel if needed, make sure the message is in
   the loaded window (fetching a history page around its seq otherwise), then
   request a jump — MessageList scrolls + flashes the row when it appears.
   Esc or clicking outside closes the panel. */

import { useEffect, useRef, useState } from "react";

import { ApiError, messageBySeq, search } from "../api";
import { stripEmotionTags } from "./Markdown";
import { useChannels } from "../stores/channels";
import { useMessages } from "../stores/messages";
import type { SearchResult } from "../types";

const DEBOUNCE_MS = 300;
const MIN_CHARS = 2;

/**
 * "1272", "#1272", "seq 1272", "seq#1272", "#seq 1272" -> 1272; anything else
 * -> null. Digits alone count: a bare number typed into search is far more
 * often a seq than a phone number here, and the text search still runs
 * underneath, so nothing is lost if it was not.
 */
export function parseSeqQuery(q: string): number | null {
  const m = /^\s*(?:#?\s*seq\s*#?\s*|#\s*)?(\d{1,9})\s*$/i.exec(q);
  if (m === null) return null;
  const n = Number(m[1]);
  return Number.isSafeInteger(n) && n >= 1 ? n : null;
}
const SNIPPET_RADIUS = 36;

/** Context snippet around the first query-term hit, term wrapped in <mark>. */
function Snippet({ content, query }: { content: string; query: string }) {
  const flat = stripEmotionTags(content).replace(/\s+/g, " ").trim();
  const lower = flat.toLowerCase();
  let hitStart = -1;
  let hitLen = 0;
  for (const term of query.toLowerCase().split(/\s+/).filter((t) => t.length > 0)) {
    const at = lower.indexOf(term);
    if (at !== -1 && (hitStart === -1 || at < hitStart)) {
      hitStart = at;
      hitLen = term.length;
    }
  }
  if (hitStart === -1) {
    return <span>{flat.length > 120 ? `${flat.slice(0, 119)}…` : flat}</span>;
  }
  const from = Math.max(0, hitStart - SNIPPET_RADIUS);
  const to = Math.min(flat.length, hitStart + hitLen + SNIPPET_RADIUS * 2);
  return (
    <span>
      {from > 0 && "…"}
      {flat.slice(from, hitStart)}
      <mark>{flat.slice(hitStart, hitStart + hitLen)}</mark>
      {flat.slice(hitStart + hitLen, to)}
      {to < flat.length && "…"}
    </span>
  );
}

function resultDate(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  return date.toLocaleDateString(undefined, {
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
    month: "short",
    day: "numeric",
  });
}

type PanelState =
  | { kind: "closed" }
  | { kind: "loading" }
  | {
      kind: "results";
      query: string;
      results: SearchResult[];
      /** The message the query names by seq, pinned above the text hits. */
      seqHit?: SearchResult | null;
      seqAsked?: number;
    }
  | { kind: "error"; detail: string };

export function SearchBar() {
  // Sidebar channel rows carry viewer-scoped names (a DM's channel.name is
  // NULL server-side — the other participant's name comes from this list).
  const sidebarChannels = useChannels((s) => s.channels);
  const [value, setValue] = useState("");
  const [panel, setPanel] = useState<PanelState>({ kind: "closed" });
  /** <768px only: whether the collapsed bar is expanded to an input. */
  const [expanded, setExpanded] = useState(false);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seqRef = useRef(0); // stale-response guard

  const close = () => {
    if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    seqRef.current += 1;
    setPanel({ kind: "closed" });
  };

  const collapse = () => {
    close();
    setExpanded(false);
  };

  useEffect(() => {
    return () => {
      if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    };
  }, []);

  const run = (query: string) => {
    const trimmed = query.trim();
    if (trimmed.length < MIN_CHARS) {
      close();
      return;
    }
    const mySeq = ++seqRef.current;
    setPanel((p) => (p.kind === "results" ? p : { kind: "loading" }));

    // A query that IS a seq — "1272", "#1272", "seq 1272" — gets two answers:
    // the one message that seq names (pinned first), then every message that
    // mentions the number (the ordinary text search). The exact hit is looked
    // up in the channel you're in, because "seq 1272" without a channel is
    // ambiguous by construction — every channel numbers from 1 — and the one
    // you have open is what you almost certainly mean. Falls through to
    // #custodian if you're somewhere seqs are rarely cited, since that is
    // where confirm records and "at seq N" live.
    const seqAsked = parseSeqQuery(trimmed);
    const seqLookup: Promise<SearchResult | null> = (() => {
      if (seqAsked === null) return Promise.resolve(null);
      const chans = useChannels.getState();
      const active = chans.channels.find((c) => c.id === chans.activeChannelId);
      const custodian = chans.channels.find((c) => c.name === "custodian");
      const first = active ?? custodian;
      if (first === undefined) return Promise.resolve(null);
      return messageBySeq(first, seqAsked).then((hit) => {
        if (hit !== null || custodian === undefined || custodian.id === first.id) return hit;
        return messageBySeq(custodian, seqAsked);
      }).catch(() => null);
    })();
    // The text search for a seq query drops the "#"/"seq" and keeps the digits:
    // FTS tokenises "1272" as its own word, so this finds every mention.
    const textQuery = seqAsked === null ? trimmed : String(seqAsked);

    Promise.all([search(textQuery), seqLookup]).then(
      ([results, seqHit]) => {
        if (seqRef.current !== mySeq) return;
        // Don't list the pinned message twice.
        const rest = seqHit === null
          ? results
          : results.filter((r) => r.message.id !== seqHit.message.id);
        setPanel({
          kind: "results", query: textQuery, results: rest,
          seqHit, seqAsked: seqAsked ?? undefined,
        });
      },
      (err: unknown) => {
        if (seqRef.current !== mySeq) return;
        setPanel({
          kind: "error",
          detail: err instanceof ApiError ? err.detail : "Search failed",
        });
      },
    );
  };

  const onChange = (next: string) => {
    setValue(next);
    if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    if (next.trim().length < MIN_CHARS) {
      close();
      return;
    }
    debounceRef.current = setTimeout(() => run(next), DEBOUNCE_MS);
  };

  const goTo = (r: SearchResult) => {
    collapse();
    const channels = useChannels.getState();
    if (channels.activeChannelId !== r.channel.id) {
      channels.setActive(r.channel.id); // AppShell effect syncs hash/focus/load
    }
    const messages = useMessages.getState();
    void messages
      .ensureAround(r.channel.id, r.message.id, r.message.seq)
      .catch(() => {
        /* history fetch failed — jump falls back to "not found" (no flash) */
      })
      .finally(() => {
        messages.requestJump(r.channel.id, r.message.id);
      });
  };

  const open = panel.kind !== "closed";

  return (
    <div className={`topbar-search${expanded ? " expanded" : ""}`}>
      <button
        className="icon-btn search-toggle"
        title="Search messages"
        aria-label="Search messages"
        onClick={() => {
          setExpanded(true);
          requestAnimationFrame(() => inputRef.current?.focus());
        }}
      >
        🔍
      </button>
      <div className="search-field">
        <input
          ref={inputRef}
          className="search-input"
          type="search"
          placeholder="Search messages"
          aria-label="Search messages"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              if (debounceRef.current !== null) clearTimeout(debounceRef.current);
              run(value);
            } else if (e.key === "Escape") {
              if (open) close();
              else {
                setValue("");
                collapse();
                (e.target as HTMLInputElement).blur();
              }
            }
          }}
          onFocus={() => {
            // Reopen previous results when tabbing back in.
            if (value.trim().length >= MIN_CHARS && panel.kind === "closed") {
              run(value);
            }
          }}
        />
        {(value.length > 0 || expanded) && (
          <button
            className="icon-btn search-clear"
            title="Clear search"
            aria-label="Clear search"
            onClick={() => {
              setValue("");
              collapse();
            }}
          >
            ✕
          </button>
        )}
      </div>

      {open && (
        <>
          <div className="search-scrim" onClick={close} />
          <div className="search-panel" role="listbox" aria-label="Search results">
            {panel.kind === "loading" && (
              <p className="search-note">Searching…</p>
            )}
            {panel.kind === "error" && (
              <p className="search-note error">{panel.detail}</p>
            )}
            {panel.kind === "results" &&
              panel.results.length === 0 &&
              !panel.seqHit && (
                <p className="search-note">
                  {panel.seqAsked !== undefined
                    ? `No message #${panel.seqAsked} in this channel, and nothing mentions it.`
                    : `No results for “${panel.query}”.`}
                </p>
              )}
            {panel.kind === "results" && panel.seqHit && (
              <>
                <p className="search-note search-section">The message itself</p>
                <button
                  key={`seq-${panel.seqHit.message.id}`}
                  className="search-row search-row-seq"
                  role="option"
                  aria-selected={false}
                  onClick={() => goTo(panel.seqHit!)}
                >
                  <span className="search-row-meta">
                    <span className="search-row-seqchip">#{panel.seqHit.message.seq}</span>
                    <span className="search-row-channel">
                      {panel.seqHit.channel.type !== "dm_1to1"
                        ? `#${panel.seqHit.channel.name ?? "main"}`
                        : "@DM"}
                    </span>
                    <span className="search-row-author">
                      {panel.seqHit.message.author.name}
                    </span>
                    <span className="search-row-date">
                      {resultDate(panel.seqHit.message.created_at)}
                    </span>
                  </span>
                  <span className="search-row-snippet">
                    {panel.seqHit.message.content.slice(0, 220)}
                    {panel.seqHit.message.content.length > 220 ? "…" : ""}
                  </span>
                </button>
                {panel.results.length > 0 && (
                  <p className="search-note search-section">
                    Messages that mention #{panel.seqAsked}
                  </p>
                )}
              </>
            )}
            {panel.kind === "results" &&
              panel.seqAsked !== undefined &&
              !panel.seqHit &&
              panel.results.length > 0 && (
                <p className="search-note search-section">
                  No message #{panel.seqAsked} in this channel — but these mention it
                </p>
              )}
            {panel.kind === "results" &&
              panel.results.map((r) => (
                <button
                  key={r.message.id}
                  className="search-row"
                  role="option"
                  aria-selected={false}
                  onClick={() => goTo(r)}
                >
                  <span className="search-row-meta">
                    <span className="search-row-channel">
                      {r.channel.type !== "dm_1to1"
                        ? `#${r.channel.name ?? "main"}`
                        : `@${
                            sidebarChannels.find((c) => c.id === r.channel.id)
                              ?.name ??
                            r.channel.name ??
                            "DM"
                          }`}
                    </span>
                    <span className="search-row-author">{r.message.author.name}</span>
                    <span className="search-row-date">
                      {resultDate(r.message.created_at)}
                    </span>
                  </span>
                  <span className="search-row-snippet">
                    <Snippet content={r.message.content} query={panel.query} />
                  </span>
                </button>
              ))}
          </div>
        </>
      )}
    </div>
  );
}
