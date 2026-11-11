/* Which clipboard contents a paste into the composer turns into attachments.

   Plain text wins: a clipboard carrying both text and an image (a spreadsheet
   range, a document selection) is a text paste, and the image is only a
   rendering of it. The one exception is text that merely names the clipboard's
   own files, which is how file managers describe a copied file. */

export interface FileLike {
  name: string;
  type: string;
}

export interface ClipboardLike<F extends FileLike> {
  getData(format: string): string;
  files: ArrayLike<F>;
  items: ArrayLike<{ kind: string; type: string; getAsFile(): F | null }>;
}

export interface PastedAttachment<F extends FileLike> {
  file: F;
  /** The name to upload under; differs from `file.name` for unnamed pastes. */
  name: string;
}

const EXT_BY_SUBTYPE: Record<string, string> = { jpeg: "jpg", "svg+xml": "svg" };

/** Browsers hand clipboard bitmaps over as `image.<ext>`, or with no name. */
function isGenericName(name: string): boolean {
  return name === "" || /^image\.[a-z0-9]+$/i.test(name);
}

function extensionOf(file: FileLike): string {
  const fromName = /\.([a-z0-9]+)$/i.exec(file.name)?.[1];
  if (fromName !== undefined) return fromName.toLowerCase();
  const subtype = file.type.split("/")[1]?.split(";")[0]?.trim().toLowerCase() ?? "";
  const ext = EXT_BY_SUBTYPE[subtype] ?? subtype;
  return /^[a-z0-9]+$/.test(ext) ? ext : "png";
}

function stamp(now: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}` +
    `-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`
  );
}

function pastedName(file: FileLike, now: Date, taken: Set<string>): string {
  const ext = extensionOf(file);
  const base = `pasted-${stamp(now)}`;
  let name = `${base}.${ext}`;
  for (let n = 2; taken.has(name); n++) name = `${base}-${n}.${ext}`;
  return name;
}

function baseName(line: string): string {
  const last = line.split(/[\\/]/).pop() ?? "";
  try {
    return decodeURIComponent(last);
  } catch {
    return last;
  }
}

/** True when every line of `text` is the name or path of one of `files`. */
function textOnlyNamesFiles(text: string, files: FileLike[]): boolean {
  const names = new Set(files.map((f) => f.name));
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l !== "");
  return lines.every((l) => names.has(l) || names.has(baseName(l)));
}

/**
 * The image files a paste should attach, each with the name to upload under.
 * An empty result means the paste is not ours and must be left to the browser.
 * `taken` is the names already staged, so same-second pastes stay distinct.
 */
export function pastedImages<F extends FileLike>(
  clip: ClipboardLike<F>,
  now: Date,
  taken: Iterable<string> = [],
): PastedAttachment<F>[] {
  let files = Array.from(clip.files);
  if (files.length === 0) {
    files = Array.from(clip.items)
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter((f): f is F => f !== null);
  }
  const images = files.filter((f) => f.type.startsWith("image/"));
  if (images.length === 0) return [];

  const text = clip.getData("text/plain").trim();
  if (text !== "" && !textOnlyNamesFiles(text, files)) return [];

  const names = new Set(taken);
  return images.map((file) => {
    const name = isGenericName(file.name) ? pastedName(file, now, names) : file.name;
    names.add(name);
    return { file, name };
  });
}
