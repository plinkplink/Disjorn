/* Compact link-unfurl card (Architecture §5.6). Lazily fetches OG metadata
   through the useUnfurl cache; renders nothing when the page yields nothing.
   The ✨ button opens the Summarize modal (state lifted to ChatView).
   A YouTube link gets a poster that swaps itself for the player on click. */

import { useEffect, useState } from "react";

import { useUnfurl } from "../stores/unfurl";
import type { UnfurlData, UnfurlEmbed } from "../types";

export interface UnfurlCardProps {
  url: string;
  onSummarize: (url: string) => void;
}

// The id lands in an iframe URL, so it is checked here as well as on the server.
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

export function UnfurlCard({ url, onSummarize }: UnfurlCardProps) {
  const entry = useUnfurl((s) => s.byUrl[url]);
  const ensure = useUnfurl((s) => s.ensure);

  useEffect(() => {
    ensure(url);
  }, [url, ensure]);

  if (entry === undefined || entry.status !== "done" || entry.data === null) {
    return null;
  }
  const { embed } = entry.data;
  if (embed?.provider === "youtube" && VIDEO_ID.test(embed.video_id)) {
    return (
      <YouTubeCard
        url={url}
        data={entry.data}
        embed={embed}
        onSummarize={onSummarize}
      />
    );
  }
  const { title, description, image_url } = entry.data;
  if (title === null && description === null && image_url === null) return null;

  return (
    <div className="unfurl-card">
      <div className="unfurl-text">
        {title !== null && (
          <a
            className="unfurl-title"
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {title}
          </a>
        )}
        {description !== null && <p className="unfurl-desc">{description}</p>}
      </div>
      {image_url !== null && (
        <img className="unfurl-thumb" src={image_url} alt="" loading="lazy" />
      )}
      <SummarizeButton url={url} onSummarize={onSummarize} />
    </div>
  );
}

function SummarizeButton({ url, onSummarize }: UnfurlCardProps) {
  return (
    <button
      className="unfurl-summarize"
      title="Summarize this link"
      aria-label="Summarize this link"
      onClick={() => onSummarize(url)}
    >
      ✨
    </button>
  );
}

interface YouTubeCardProps extends UnfurlCardProps {
  data: UnfurlData;
  embed: UnfurlEmbed;
}

function YouTubeCard({ url, data, embed, onSummarize }: YouTubeCardProps) {
  const [playing, setPlaying] = useState(false);
  const title = data.title ?? "YouTube video";
  const thumb =
    data.image_url ?? `https://i.ytimg.com/vi/${embed.video_id}/hqdefault.jpg`;
  const start =
    embed.start_seconds !== null && Number.isInteger(embed.start_seconds)
      ? `&start=${embed.start_seconds}`
      : "";

  return (
    <div className="unfurl-card unfurl-video">
      <div className="unfurl-text">
        {data.description !== null && (
          <p className="unfurl-channel">{data.description}</p>
        )}
        <a
          className="unfurl-title"
          href={url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {title}
        </a>
      </div>
      {playing ? (
        <iframe
          className="unfurl-video-frame"
          src={`https://www.youtube-nocookie.com/embed/${embed.video_id}?autoplay=1${start}`}
          title={title}
          allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
          allowFullScreen
          referrerPolicy="strict-origin-when-cross-origin"
          loading="lazy"
        />
      ) : (
        <button
          className="unfurl-video-frame unfurl-video-poster"
          aria-label={`Play ${title}`}
          onClick={() => setPlaying(true)}
        >
          <img src={thumb} alt="" loading="lazy" />
          <svg className="unfurl-play" viewBox="0 0 68 48" aria-hidden="true">
            <rect width="68" height="48" rx="12" fill="#f00" />
            <path d="M27 14v20l18-10z" fill="#fff" />
          </svg>
        </button>
      )}
      <SummarizeButton url={url} onSummarize={onSummarize} />
    </div>
  );
}
