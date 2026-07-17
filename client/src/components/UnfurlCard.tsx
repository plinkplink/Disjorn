/* Compact link-unfurl card (Architecture §5.6). Lazily fetches OG metadata
   through the useUnfurl cache; renders nothing when the page yields nothing.
   The ✨ button opens the Summarize modal (state lifted to ChatView). */

import { useEffect } from "react";

import { useUnfurl } from "../stores/unfurl";

export interface UnfurlCardProps {
  url: string;
  onSummarize: (url: string) => void;
}

export function UnfurlCard({ url, onSummarize }: UnfurlCardProps) {
  const entry = useUnfurl((s) => s.byUrl[url]);
  const ensure = useUnfurl((s) => s.ensure);

  useEffect(() => {
    ensure(url);
  }, [url, ensure]);

  if (entry === undefined || entry.status !== "done" || entry.data === null) {
    return null;
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
      <button
        className="unfurl-summarize"
        title="Summarize this link"
        aria-label="Summarize this link"
        onClick={() => onSummarize(url)}
      >
        ✨
      </button>
    </div>
  );
}
