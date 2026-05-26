// Batch-encode helper for the composer. Each file goes through the
// resize+WebP pipeline; errors on individual files don't block the
// rest of the batch.

import { resizeAndEncode, type ImageAttachment } from "./image-attach";

export async function encodeFiles(files: File[]): Promise<ImageAttachment[]> {
  const out: ImageAttachment[] = [];
  for (const f of files) {
    try {
      out.push(await resizeAndEncode(f));
    } catch (err) {
      console.warn("[voitta] image encode failed", f.name, err);
    }
  }
  return out;
}
