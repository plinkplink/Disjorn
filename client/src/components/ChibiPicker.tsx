/* Admin chibi picker: choose the face a bot's `[emotion: X]` tag gets.

   Picking a face saves the alias for the whole pack (every later "wry" from
   any bot on that pack) and, with the box ticked, re-points this message too.
   Older messages keep the face they showed. A popover beside the chibi on a
   wide screen, a bottom sheet under 600 px. */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";

import {
  ApiError,
  chibiAlias,
  chibiFaces,
  listBots,
  removeChibiAlias,
  repointEmote,
  setChibiAlias,
} from "../api";
import { useMessages } from "../stores/messages";
import type { Bot, ChibiAliasState, ChibiFace, Message } from "../types";

export interface ChibiPickRequest {
  message: Message;
  /** Which of the message's tags, in document order. */
  index: number;
  tag: string;
  /** The face this tag shows right now, or null for none. */
  current: string | null;
  anchor: DOMRect;
}

interface ChibiPickerProps {
  request: ChibiPickRequest;
  onClose: () => void;
  onDone: (toast: string) => void;
}

const isCoarsePointer =
  typeof window !== "undefined" &&
  window.matchMedia("(pointer: coarse)").matches;

const GAP_PX = 8;

let botsOnce: Promise<Bot[]> | null = null;

/** The pack a bot's message resolves against, by the name its URLs use. */
async function packFor(botId: number): Promise<string | null> {
  botsOnce ??= listBots().catch((err: unknown) => {
    botsOnce = null;
    throw err;
  });
  const bot = (await botsOnce).find((b) => b.id === botId);
  const pack = bot?.chibi_pack ?? null;
  return pack === null ? null : (pack.split(/[\\/]/).filter(Boolean).pop() ?? null);
}

const norm = (s: string): string => s.toLowerCase().replace(/[^a-z0-9]+/g, "");

function categoryLabel(category: string): string {
  return category.replace(/_/g, " ");
}

function detail(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

export function ChibiPicker({ request, onClose, onDone }: ChibiPickerProps) {
  const { message, index, tag, current, anchor } = request;
  const [pack, setPack] = useState<string | null>(null);
  const [faces, setFaces] = useState<ChibiFace[] | null>(null);
  const [state, setState] = useState<ChibiAliasState | null>(null);
  const [filter, setFilter] = useState("");
  const [active, setActive] = useState(0);
  const [alsoThis, setAlsoThis] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [place, setPlace] = useState<CSSProperties>({});

  const panelRef = useRef<HTMLDivElement | null>(null);
  const filterRef = useRef<HTMLInputElement | null>(null);
  const faceRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const p = await packFor(message.author_id);
        if (p === null) throw new ApiError(400, "This bot has no chibi pack");
        const [f, s] = await Promise.all([chibiFaces(p), chibiAlias(p, tag)]);
        if (!live) return;
        setPack(p);
        setFaces(f);
        setState(s);
      } catch (err) {
        if (live) setError(detail(err, "Couldn't load the pack"));
      }
    })();
    return () => {
      live = false;
    };
  }, [message.author_id, tag]);

  // Focus moves in on open and back to the face button on close.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    if (isCoarsePointer) panelRef.current?.focus();
    else filterRef.current?.focus();
    return () => before?.focus();
  }, []);

  const shown = useMemo(() => {
    if (faces === null) return [];
    const q = norm(filter);
    return q === "" ? faces : faces.filter((f) => norm(f.name).includes(q));
  }, [faces, filter]);

  const groups = useMemo(() => {
    const out: { category: string; items: { face: ChibiFace; i: number }[] }[] = [];
    shown.forEach((face, i) => {
      const last = out[out.length - 1];
      if (last !== undefined && last.category === face.category) {
        last.items.push({ face, i });
      } else {
        out.push({ category: face.category, items: [{ face, i }] });
      }
    });
    return out;
  }, [shown]);

  useEffect(() => setActive(0), [filter]);

  useEffect(() => {
    faceRefs.current[active]?.scrollIntoView({ block: "nearest" });
  }, [active]);

  // Below the anchor when it fits, above it otherwise, always on screen.
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (panel === null) return;
    const h = panel.offsetHeight;
    const w = panel.offsetWidth;
    const below = anchor.bottom + GAP_PX;
    const top =
      below + h <= window.innerHeight - GAP_PX
        ? below
        : Math.max(GAP_PX, anchor.top - GAP_PX - h);
    const left = Math.min(
      Math.max(GAP_PX, anchor.left),
      window.innerWidth - w - GAP_PX,
    );
    setPlace({ "--picker-top": `${top}px`, "--picker-left": `${left}px` } as CSSProperties);
  }, [anchor, faces, error]);

  const ownName = state?.source === "name";

  const pick = async (face: ChibiFace) => {
    if (pack === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      let saved = tag;
      if (!ownName) saved = (await setChibiAlias(pack, tag, face.name)).tag;
      if (alsoThis || ownName) {
        const updated = await repointEmote(message.id, tag, index, face.name);
        useMessages.getState().applyEdit(updated);
      }
      onDone(
        ownName
          ? `This message now shows ${face.name}`
          : `"${saved}" now shows ${face.name} for ${pack}`,
      );
    } catch (err) {
      setError(detail(err, "Couldn't save that face"));
      setBusy(false);
    }
  };

  const removeAlias = async () => {
    if (pack === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await removeChibiAlias(pack, tag);
      onDone(`"${r.tag}" is back on the ladder for ${pack} (${r.face ?? "no face"})`);
    } catch (err) {
      setError(detail(err, "Couldn't remove the alias"));
      setBusy(false);
    }
  };

  /** The face straight above or below the active one, by where it sits on
      screen, so the move crosses category headers the way the eye does. */
  const vertical = (dir: 1 | -1): number => {
    const from = faceRefs.current[active]?.getBoundingClientRect();
    if (from === undefined) return active;
    const cx = from.left + from.width / 2;
    let best = active;
    let bestRow = Infinity;
    let bestDx = Infinity;
    faceRefs.current.forEach((el, i) => {
      if (el === null || i >= shown.length) return;
      const r = el.getBoundingClientRect();
      const dy = (r.top - from.top) * dir;
      if (dy <= 1) return;
      const dx = Math.abs(r.left + r.width / 2 - cx);
      if (dy < bestRow - 1 || (Math.abs(dy - bestRow) <= 1 && dx < bestDx)) {
        best = i;
        bestRow = dy;
        bestDx = dx;
      }
    });
    return best;
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const inFilter = e.target === filterRef.current;
    const caret = inFilter && filter !== "";
    let next: number | null = null;
    switch (e.key) {
      case "Escape":
        e.preventDefault();
        onClose();
        return;
      case "Enter": {
        const face = shown[active];
        if ((inFilter || e.target === panelRef.current) && face !== undefined) {
          e.preventDefault();
          void pick(face);
        }
        return;
      }
      case "ArrowRight":
        if (!caret) next = Math.min(active + 1, shown.length - 1);
        break;
      case "ArrowLeft":
        if (!caret) next = Math.max(active - 1, 0);
        break;
      case "ArrowDown":
        next = vertical(1);
        break;
      case "ArrowUp":
        next = vertical(-1);
        break;
      default:
        return;
    }
    if (next === null || shown.length === 0) return;
    e.preventDefault();
    setActive(next);
    if (!inFilter) faceRefs.current[next]?.focus();
  };

  faceRefs.current.length = shown.length;

  return createPortal(
    <>
      <div className="chibi-picker-scrim" onClick={onClose} />
      <div
        ref={panelRef}
        className="chibi-picker"
        style={place}
        role="dialog"
        aria-label={`Choose a face for "${tag}"`}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="chibi-picker-head">
          <span className="chibi-picker-title">
            "{tag}" → {current ?? "no face"}
          </span>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <input
          ref={filterRef}
          className="chibi-picker-filter"
          type="search"
          placeholder="Filter faces"
          aria-label="Filter faces"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <div className="chibi-picker-body" role="listbox" aria-label="Faces">
          {faces === null && error === null && (
            <p className="chibi-picker-note">Loading faces…</p>
          )}
          {faces !== null && shown.length === 0 && (
            <p className="chibi-picker-note">No face matches "{filter}".</p>
          )}
          {groups.map((g) => (
            <section key={g.category} className="chibi-picker-group">
              <h4>{categoryLabel(g.category)}</h4>
              <div className="chibi-picker-grid">
                {g.items.map(({ face, i }) => (
                  <button
                    key={face.url}
                    ref={(el) => {
                      faceRefs.current[i] = el;
                    }}
                    className={`chibi-picker-face${i === active ? " active" : ""}${
                      face.name === current ? " current" : ""
                    }`}
                    role="option"
                    aria-selected={i === active}
                    tabIndex={i === active ? 0 : -1}
                    title={face.name}
                    disabled={busy}
                    onClick={() => void pick(face)}
                    onFocus={() => setActive(i)}
                  >
                    <img src={face.url} alt="" loading="lazy" />
                    <span>{face.name}</span>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
        {ownName && (
          <p className="chibi-picker-note">
            "{tag}" is a face's own name, so no alias can change it. Picking
            changes only this message.
          </p>
        )}
        {error !== null && (
          <p className="chibi-picker-error" role="alert">
            {error}
          </p>
        )}
        <div className="chibi-picker-foot">
          <label className="chibi-picker-also">
            <input
              type="checkbox"
              checked={alsoThis || ownName}
              disabled={ownName}
              onChange={(e) => setAlsoThis(e.target.checked)}
            />
            Also fix this message
          </label>
          <button
            className="btn"
            disabled={busy || state?.source !== "alias"}
            title={
              state?.source === "alias"
                ? "Delete this tag's alias line; the ladder picks again"
                : "This tag has no alias"
            }
            onClick={() => void removeAlias()}
          >
            Remove alias
          </button>
        </div>
      </div>
    </>,
    document.body,
  );
}
