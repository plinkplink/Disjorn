// Generates PWA icons (PNG) with no external deps: raw RGBA pixels drawn with
// per-pixel math (rounded-rect background + geometric "D" glyph), encoded as
// PNG by hand (IHDR/IDAT/IEND + CRC32, zlib deflate from node:zlib).
//
// Usage: node scripts/gen-icons.mjs   (writes into public/icons/)

import { deflateSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "icons");

// Palette (matches theme.css tokens)
const BG = [30, 31, 36]; // --bg-0
const ACCENT = [124, 108, 255]; // --accent

const crcTable = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function encodePng(size, rgba) {
  // One filter byte (0 = None) per scanline.
  const raw = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) {
    const rowStart = y * (size * 4 + 1);
    raw[rowStart] = 0;
    rgba.copy(raw, rowStart + 1, y * size * 4, (y + 1) * size * 4);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/**
 * Signed-distance-ish coverage for the icon at a unit coordinate (0..1).
 * Returns [r, g, b, a].
 *
 * The "D": a vertical stem plus a right half-annulus, drawn in accent-tinted
 * near-white on the accent rounded-rect (or full-bleed for maskable).
 */
function pixel(u, v, { maskable }) {
  // Background: rounded rect (corner radius 22%) or full bleed for maskable.
  let bgA = 1;
  if (!maskable) {
    const r = 0.22;
    const cx = Math.max(Math.abs(u - 0.5) - (0.5 - r), 0);
    const cy = Math.max(Math.abs(v - 0.5) - (0.5 - r), 0);
    const d = Math.hypot(cx, cy) - r;
    bgA = Math.min(Math.max(0.5 - d * 200, 0), 1); // hard-ish AA edge
    if (bgA === 0) return [0, 0, 0, 0];
  }

  // Glyph box: maskable gets a smaller glyph (safe zone is the inner 80%).
  const s = maskable ? 0.62 : 0.78; // glyph scale
  const gx = (u - 0.5) / s + 0.5;
  const gy = (v - 0.5) / s + 0.5;

  const stemL = 0.22;
  const stemR = 0.40;
  const top = 0.14;
  const bottom = 0.86;
  const cy0 = 0.5;
  const outerR = (bottom - top) / 2; // 0.36
  const innerR = outerR - 0.18;
  const bowlCx = stemR - 0.02;

  let inGlyph = false;
  // Stem
  if (gx >= stemL && gx <= stemR && gy >= top && gy <= bottom) inGlyph = true;
  // Bowl: right half-annulus
  if (!inGlyph && gx > bowlCx) {
    const d = Math.hypot(gx - bowlCx, gy - cy0);
    if (d <= outerR && d >= innerR) inGlyph = true;
  }

  if (inGlyph) {
    // near-white glyph
    return [240, 240, 250, Math.round(bgA * 255)];
  }
  const [r, g, b] = maskable ? BG : accentMix(v);
  return [r, g, b, Math.round(bgA * 255)];

  function accentMix(t) {
    // subtle vertical gradient on the accent tile
    const k = 0.85 + 0.15 * (1 - t);
    return [ACCENT[0] * k, ACCENT[1] * k, ACCENT[2] * k].map(Math.round);
  }
}

function render(size, opts) {
  const rgba = Buffer.alloc(size * size * 4);
  const SS = 3; // supersampling
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0,
        g = 0,
        b = 0,
        a = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const [pr, pg, pb, pa] = pixel(
            (x + (sx + 0.5) / SS) / size,
            (y + (sy + 0.5) / SS) / size,
            opts,
          );
          r += pr * pa;
          g += pg * pa;
          b += pb * pa;
          a += pa;
        }
      }
      const n = SS * SS;
      const i = (y * size + x) * 4;
      rgba[i] = a ? Math.round(r / a) : 0;
      rgba[i + 1] = a ? Math.round(g / a) : 0;
      rgba[i + 2] = a ? Math.round(b / a) : 0;
      rgba[i + 3] = Math.round(a / n);
    }
  }
  return encodePng(size, rgba);
}

mkdirSync(OUT_DIR, { recursive: true });
writeFileSync(join(OUT_DIR, "icon-192.png"), render(192, { maskable: false }));
writeFileSync(join(OUT_DIR, "icon-512.png"), render(512, { maskable: false }));
// Maskable: glyph on a full-bleed dark tile so any mask shape works.
writeFileSync(
  join(OUT_DIR, "icon-512-maskable.png"),
  render(512, { maskable: true }),
);
console.log("icons written to", OUT_DIR);
