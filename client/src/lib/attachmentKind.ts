/* How a message shows one attachment. The server serves inline only a file it
   decoded as an image (`width` is set) or recognised as audio or video; every
   other file answers as a download, so a link to it must not expect a page. */

import type { Attachment } from "../types";

export type AttachmentKind = "image" | "media" | "download";

export function attachmentKind(
  att: Pick<Attachment, "mime_type" | "width" | "url">,
): AttachmentKind {
  if (att.url === null) return "download";
  if (att.width !== null) return "image";
  return /^(audio|video)\//i.test(att.mime_type) ? "media" : "download";
}
