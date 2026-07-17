/* Full-size image preview modal (Architecture §6): display variant, link to
   the original, filename. Esc or backdrop click closes. */

import { useEffect } from "react";

export interface ImageModalProps {
  /** Display-variant URL (signed media URL or picker asset). */
  src: string;
  /** Best available "original" link — falls back to src upstream. */
  origUrl: string;
  filename: string;
  onClose: () => void;
}

export function ImageModal({ src, origUrl, filename, onClose }: ImageModalProps) {
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
          <a href={origUrl} target="_blank" rel="noopener noreferrer">
            Open original
          </a>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
      </div>
    </div>
  );
}
