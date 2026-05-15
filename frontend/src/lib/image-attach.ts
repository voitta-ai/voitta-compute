// Resize-and-encode pipeline for chat image attachments.
//
// 1. Decode the file via `createImageBitmap` (handles HEIC, AVIF, etc. if
//    the browser does — Safari 17+, Chrome).
// 2. Scale so the longest edge is at most MAX_EDGE, preserving aspect.
// 3. Re-encode as WebP @ q=0.85 via an offscreen canvas. Browsers that
//    don't support WebP encoding (Safari < 16) silently fall back to PNG
//    inside `toBlob`; we read the actual `blob.type` rather than assuming.
// 4. Return both the base64 payload (for the wire) and a `data:` URL
//    (for inline rendering in `<img>` tags).
//
// 1024 px / q=0.85 lands a typical photo around 80–300 KB, well under
// any model provider's per-image cap. Anything larger raises an error
// the composer can surface.

const MAX_EDGE = 1024;
const WEBP_QUALITY = 0.85;
const MAX_BYTES_AFTER_ENCODE = 2 * 1024 * 1024; // 2 MB

export interface ImageAttachment {
  /** MIME of the encoded blob — usually "image/webp", "image/png" on fallback. */
  mime: string;
  /** Base64 payload, no `data:` prefix. */
  data: string;
  /** Post-resize dimensions. Useful for the composer's thumbnail layout. */
  width: number;
  height: number;
  /** Full `data:<mime>;base64,<data>` URL for `<img src=…>`. */
  dataUrl: string;
}

export async function resizeAndEncode(file: File): Promise<ImageAttachment> {
  if (!file.type.startsWith("image/")) {
    throw new Error(`not an image: ${file.type || "unknown"}`);
  }
  const bitmap = await createImageBitmap(file);
  const longEdge = Math.max(bitmap.width, bitmap.height);
  const scale = longEdge > MAX_EDGE ? MAX_EDGE / longEdge : 1;
  const w = Math.max(1, Math.round(bitmap.width * scale));
  const h = Math.max(1, Math.round(bitmap.height * scale));

  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no 2D context");
  ctx.drawImage(bitmap, 0, 0, w, h);
  bitmap.close?.();

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error("canvas.toBlob returned null"))),
      "image/webp",
      WEBP_QUALITY,
    );
  });
  if (blob.size > MAX_BYTES_AFTER_ENCODE) {
    throw new Error(
      `image too large after encode: ${Math.round(blob.size / 1024)} KB`,
    );
  }
  const mime = blob.type || "image/png";
  const data = await _blobToBase64(blob);
  return { mime, data, width: w, height: h, dataUrl: `data:${mime};base64,${data}` };
}

function _blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("FileReader returned non-string"));
        return;
      }
      // result is "data:<mime>;base64,<payload>" — strip the prefix.
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error ?? new Error("FileReader failed"));
    reader.readAsDataURL(blob);
  });
}

/** Pull image files out of a DataTransfer / ClipboardData item list. */
export function extractImageFiles(items: DataTransferItemList | null): File[] {
  if (!items) return [];
  const out: File[] = [];
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind === "file" && it.type.startsWith("image/")) {
      const f = it.getAsFile();
      if (f) out.push(f);
    }
  }
  return out;
}
