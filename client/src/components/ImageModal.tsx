/* Full-size image preview modal (Architecture §6): display variant, link to
   the preserved original when the server offers one, filename. Esc or backdrop
   click closes. */

import { useEffect } from "react";

export interface ImageModalProps {
  /** Display-variant URL (signed media URL or picker asset). */
  src: string;
  /**
   * Signed URL of the preserved upload (`attachment.orig_url`). Optional: the
   * payload gained this field late, so messages from an older payload — and
   * picker images, which have no original — simply don't offer the link.
   */
  origUrl?: string | null;
  filename: string;
  onClose: () => void;
}

export function ImageModal({ src, origUrl, filename, onClose }: ImageModalProps) {
  // Only a *different* URL is worth a second link: the display variant is
  // already what the modal is showing.
  const hasOriginal =
    typeof origUrl === "string" && origUrl.length > 0 && origUrl !== src;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="image-modal"
        role="dialog"
        aria-label={filename}
        onClick={(e) => e.stopPropagation()}
      >
        <img className="image-modal-img" src={src} alt={filename} />
        <div className="image-modal-bar">
          <span className="image-modal-name" title={filename}>
            {filename}
          </span>
          <a href={src} target="_blank" rel="noopener noreferrer">
            Open image
          </a>
          {hasOriginal && (
            <a
              className="image-modal-orig"
              href={origUrl ?? src}
              target="_blank"
              rel="noopener noreferrer"
              title="The file exactly as uploaded, before web conversion"
            >
              View original
            </a>
          )}
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
      </div>
    </div>
  );
}
