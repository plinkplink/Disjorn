/* Discord-flavored markdown, hand-rolled. Renders straight to React elements —
   no HTML strings, no sanitizer needed, nothing user-controlled ever reaches
   the DOM as markup.

   Supported (Architecture §5.5): **bold**, *italic* / _italic_, __underline__
   (Discord-ism), ~~strike~~, `inline code`, ```fenced blocks``` (language
   label + copy button), > quotes, ||spoilers|| (click to reveal), links
   (new tab, noopener), @mentions matched against channel member names, and
   bare image/GIF URLs (picker files or image extensions) shown as inline
   images. */

import { Fragment, memo, useState } from "react";
import type { ReactNode } from "react";

/* ---------------------------------------------------------------- helpers */

const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|avif|svg)$/i;
const URL_TRAILING_PUNCT_RE = /[)\],.!?;:'"]+$/;
const HTTP_URL_RE = /https?:\/\/[^\s<>]+/;
const PICKER_URL_RE = /\/picker\/file\/(?:gif|image)\/[^\s<>]+/;

export function isImageUrl(url: string): boolean {
  const path = url.split("?")[0] ?? url;
  return path.includes("/picker/file/") || IMAGE_EXT_RE.test(path);
}

function trimUrl(raw: string): { url: string; tail: string } {
  const url = raw.replace(URL_TRAILING_PUNCT_RE, "");
  return { url, tail: raw.slice(url.length) };
}

/** First http(s) URL in a message that is NOT an inline-rendered image —
    the unfurl-card candidate. Returns null when there is none. */
export function firstHttpUrl(content: string): string | null {
  // Strip code spans/blocks first: URLs inside code never unfurl.
  const stripped = content
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`\n]*`/g, " ");
  let rest = stripped;
  for (;;) {
    const m = HTTP_URL_RE.exec(rest);
    if (m === null) return null;
    const { url } = trimUrl(m[0]);
    if (!isImageUrl(url)) return url;
    rest = rest.slice(m.index + m[0].length);
  }
}

/* --------------------------------------------------------- leaf components */

function Spoiler({ children }: { children: ReactNode }) {
  const [revealed, setRevealed] = useState(false);
  return (
    <span
      className={`md-spoiler${revealed ? " revealed" : ""}`}
      role="button"
      tabIndex={0}
      title={revealed ? undefined : "Click to reveal"}
      onClick={() => setRevealed(true)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") setRevealed(true);
      }}
    >
      {children}
    </span>
  );
}

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div className="md-codeblock">
      <div className="md-codeblock-bar">
        <span className="md-codeblock-lang">{lang || "code"}</span>
        <button className="md-codeblock-copy" onClick={copy}>
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

function UrlNode({ url }: { url: string }) {
  if (isImageUrl(url)) {
    return (
      <a href={url} target="_blank" rel="noopener noreferrer">
        <img className="md-inline-img" src={url} alt="" loading="lazy" />
      </a>
    );
  }
  return (
    <a href={url} target="_blank" rel="noopener noreferrer">
      {url}
    </a>
  );
}

/* ------------------------------------------------------------- tokenizer */

interface Ctx {
  mentionRe: RegExp | null;
}

interface PatternDef {
  re: RegExp; // non-global: .exec() finds the earliest match
  render: (m: RegExpExecArray, ctx: Ctx, key: number) => ReactNode;
}

const inner = (m: RegExpExecArray): string => m[1] ?? "";

/* Order = priority when two patterns match at the same index (e.g. `**` must
   beat `*`, `__` must beat `_`). */
const PATTERNS: PatternDef[] = [
  {
    re: /`([^`\n]+)`/,
    render: (m, _ctx, key) => (
      <code className="md-inline-code" key={key}>
        {inner(m)}
      </code>
    ),
  },
  {
    re: /\|\|([\s\S]+?)\|\|/,
    render: (m, ctx, key) => (
      <Spoiler key={key}>{parseInline(inner(m), ctx)}</Spoiler>
    ),
  },
  {
    // (?!\*) lets ***x*** resolve as bold(italic) instead of eating a star.
    re: /\*\*([\s\S]+?)\*\*(?!\*)/,
    render: (m, ctx, key) => (
      <strong key={key}>{parseInline(inner(m), ctx)}</strong>
    ),
  },
  {
    // Discord-ism: double underscore is UNDERLINE, not bold.
    re: /__([\s\S]+?)__(?!_)/,
    render: (m, ctx, key) => <u key={key}>{parseInline(inner(m), ctx)}</u>,
  },
  {
    re: /~~([\s\S]+?)~~/,
    render: (m, ctx, key) => <s key={key}>{parseInline(inner(m), ctx)}</s>,
  },
  {
    re: /\*([^\s*][^*\n]*?)\*/,
    render: (m, ctx, key) => <em key={key}>{parseInline(inner(m), ctx)}</em>,
  },
  {
    re: /(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])/,
    render: (m, ctx, key) => <em key={key}>{parseInline(inner(m), ctx)}</em>,
  },
  {
    re: HTTP_URL_RE,
    render: (m, _ctx, key) => {
      const { url, tail } = trimUrl(m[0]);
      return (
        <Fragment key={key}>
          <UrlNode url={url} />
          {tail}
        </Fragment>
      );
    },
  },
  {
    // Relative picker-file URLs (what the picker posts) render as images.
    re: PICKER_URL_RE,
    render: (m, _ctx, key) => <UrlNode key={key} url={m[0]} />,
  },
];

function mentionDef(mentionRe: RegExp): PatternDef {
  return {
    re: mentionRe,
    render: (m, _ctx, key) => (
      <span className="md-mention" key={key}>
        {m[0]}
      </span>
    ),
  };
}

function parseInline(text: string, ctx: Ctx): ReactNode[] {
  const defs =
    ctx.mentionRe !== null ? [...PATTERNS, mentionDef(ctx.mentionRe)] : PATTERNS;
  const out: ReactNode[] = [];
  let rest = text;
  let key = 0;
  while (rest.length > 0) {
    let best: { index: number; def: PatternDef; m: RegExpExecArray } | null =
      null;
    for (const def of defs) {
      const m = def.re.exec(rest);
      if (m !== null && (best === null || m.index < best.index)) {
        best = { index: m.index, def, m };
        if (m.index === 0) break; // can't do better; defs are priority-ordered
      }
    }
    if (best === null) {
      out.push(rest);
      break;
    }
    if (best.index > 0) out.push(rest.slice(0, best.index));
    out.push(best.def.render(best.m, ctx, key++));
    rest = rest.slice(best.index + best.m[0].length);
  }
  return out;
}

/* ---------------------------------------------------------------- blocks */

const FENCE_RE = /```([A-Za-z0-9+#._-]*)\n?([\s\S]*?)```/;
const QUOTE_LINE_RE = /^>\s?/;

function parseTextBlock(text: string, ctx: Ctx, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const lines = text.split("\n");
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (QUOTE_LINE_RE.test(line)) {
      const quoted: string[] = [];
      while (i < lines.length && QUOTE_LINE_RE.test(lines[i] ?? "")) {
        quoted.push((lines[i] ?? "").replace(QUOTE_LINE_RE, ""));
        i++;
      }
      out.push(
        <blockquote className="md-quote" key={`${keyBase}q${key++}`}>
          {parseInline(quoted.join("\n"), ctx)}
        </blockquote>,
      );
    } else {
      const plain: string[] = [];
      while (i < lines.length && !QUOTE_LINE_RE.test(lines[i] ?? "")) {
        plain.push(lines[i] ?? "");
        i++;
      }
      const joined = plain.join("\n");
      if (joined.length > 0) {
        out.push(
          <Fragment key={`${keyBase}t${key++}`}>
            {parseInline(joined, ctx)}
          </Fragment>,
        );
      }
    }
  }
  return out;
}

function parseBlocks(src: string, ctx: Ctx): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = src;
  let key = 0;
  while (rest.length > 0) {
    const m = FENCE_RE.exec(rest);
    if (m === null) {
      out.push(...parseTextBlock(rest, ctx, `b${key++}`));
      break;
    }
    if (m.index > 0) {
      out.push(...parseTextBlock(rest.slice(0, m.index), ctx, `b${key++}`));
    }
    const code = (m[2] ?? "").replace(/\n$/, "");
    out.push(<CodeBlock key={`c${key++}`} lang={m[1] ?? ""} code={code} />);
    rest = rest.slice(m.index + m[0].length).replace(/^\n/, "");
  }
  return out;
}

/* ------------------------------------------------------------- component */

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Build a case-insensitive `@Name` matcher; longest names win so
    "@Ann Marie" beats "@Ann". Null when there is nobody to match. */
export function buildMentionRe(names: string[] | undefined): RegExp | null {
  if (names === undefined || names.length === 0) return null;
  const parts = [...new Set(names.filter((n) => n.length > 0))]
    .sort((a, b) => b.length - a.length)
    .map(escapeRe);
  if (parts.length === 0) return null;
  return new RegExp(`@(?:${parts.join("|")})`, "i");
}

interface MarkdownProps {
  content: string;
  /** Channel member display names + usernames for @mention highlighting. */
  mentionNames?: string[];
}

export const Markdown = memo(function Markdown({
  content,
  mentionNames,
}: MarkdownProps) {
  const ctx: Ctx = { mentionRe: buildMentionRe(mentionNames) };
  return <div className="md">{parseBlocks(content, ctx)}</div>;
});
