/* Summarize modal (Architecture §5.6): POST /summarize on open, loading ->
   summary text, Close, and "Share to channel" which posts the summary as a
   quoted message to the active channel. Server 502/503 details surface as-is. */

import { useEffect, useState } from "react";

import { ApiError, sendMessage, summarizeUrl } from "../api";
import { useMessages } from "../stores/messages";

type SummarizeState =
  | { status: "loading" }
  | { status: "done"; summary: string }
  | { status: "error"; detail: string };

export interface SummarizeModalProps {
  url: string;
  /** Channel "Share to channel" posts into (the active one). */
  channelId: number;
  onClose: () => void;
}

export function SummarizeModal({ url, channelId, onClose }: SummarizeModalProps) {
  const [state, setState] = useState<SummarizeState>({ status: "loading" });
  const [sharing, setSharing] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setState({ status: "loading" });
    summarizeUrl(url).then(
      (res) => {
        if (alive) setState({ status: "done", summary: res.summary });
      },
      (err: unknown) => {
        if (alive) {
          setState({
            status: "error",
            detail:
              err instanceof ApiError ? err.detail : "Summarization failed",
          });
        }
      },
    );
    return () => {
      alive = false;
    };
  }, [url]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const share = async () => {
    if (state.status !== "done" || sharing) return;
    setSharing(true);
    setShareError(null);
    const quoted = state.summary.replace(/\n/g, "\n> ");
    try {
      const msg = await sendMessage(
        channelId,
        `**Summary of** ${url}:\n> ${quoted}`,
      );
      useMessages.getState().applyCreate(msg);
      onClose();
    } catch (err) {
      setShareError(err instanceof ApiError ? err.detail : "Failed to post");
      setSharing(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="summarize-modal"
        role="dialog"
        aria-label="Summary"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="summarize-modal-head">
          <span className="summarize-modal-title">✨ Summary</span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <a
          className="summarize-modal-url"
          href={url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {url}
        </a>
        <div className="summarize-modal-body">
          {state.status === "loading" && (
            <p className="summarize-loading">Summarizing…</p>
          )}
          {state.status === "error" && (
            <p className="form-error">{state.detail}</p>
          )}
          {state.status === "done" && <p>{state.summary}</p>}
        </div>
        {shareError !== null && <p className="form-error">{shareError}</p>}
        <div className="summarize-modal-actions">
          <button className="btn" onClick={onClose}>
            Close
          </button>
          <button
            className="btn btn-primary"
            disabled={state.status !== "done" || sharing}
            onClick={() => void share()}
          >
            {sharing ? "Posting…" : "Share to channel"}
          </button>
        </div>
      </div>
    </div>
  );
}
