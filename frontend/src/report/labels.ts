// Report titles are written by LLM-authored report scripts, which routinely
// HTML-escape text that was never HTML — a report called "Supply & Cost"
// arrives as "Supply &amp; Cost". The tab label is plain text rendered by
// React, so the entity survives to the screen verbatim.
//
// Decoding here rather than at ingestion keeps the stored title byte-exact
// (the backend still has whatever the script sent) and covers every display
// site at once. Done with a table, not an element: assigning to innerHTML to
// decode would execute markup, and this string is model-authored.

const NAMED: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  ndash: "–",
  mdash: "—",
  hellip: "…",
  middot: "·",
};

const ENTITY_RE = /&(?:#(\d{1,7})|#[xX]([0-9a-fA-F]{1,6})|([a-zA-Z][a-zA-Z0-9]{1,9}));/g;

/** Decode the HTML entities that show up in model-written titles.
 *
 * Single pass by design: "&amp;lt;" means the author wanted the literal text
 * "&lt;", so it decodes to that and stops. Unknown entities are left exactly
 * as written rather than guessed at.
 */
export function decodeEntities(text: string): string {
  if (!text || text.indexOf("&") === -1) return text;
  return text.replace(ENTITY_RE, (whole, dec?: string, hex?: string, name?: string) => {
    if (name !== undefined) {
      return NAMED[name.toLowerCase()] ?? whole;
    }
    const code = dec !== undefined ? Number(dec) : parseInt(hex as string, 16);
    // Surrogates and out-of-range code points would throw; a malformed title
    // should degrade to its raw text, never break the tab bar.
    if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff) return whole;
    if (code >= 0xd800 && code <= 0xdfff) return whole;
    try {
      return String.fromCodePoint(code);
    } catch {
      return whole;
    }
  });
}

/** The human-facing name for a report tab. */
export function reportLabel(r: { title?: string | null; name: string }): string {
  return decodeEntities((r.title ?? r.name) || r.name);
}
