// Markdown rendering for the chat surface.
//
// `marked` is sync; `mermaid.render` is async. Strategy: in `marked`'s
// `code` renderer, swap `mermaid` fenced blocks for a pending placeholder
// `<div data-mermaid="pending" data-mermaid-source="<base64>">`. Caller
// later runs `hydrateMermaid(rootEl)` to replace those placeholders with
// real SVG. The source is base64-encoded to survive the HTML attribute
// round-trip without escaping headaches.

import { marked } from "marked";
import DOMPurify from "dompurify";
import mermaid from "mermaid";

// Theme matches the rest of the widget (light surfaces, near-black accent).
// We can't read CSS variables at module-init time — theme.css is loaded
// into the shadow root, not :root — and mermaid freezes its theme at
// `initialize()`, so the values are duplicated.
let mermaidReady = false;
function ensureMermaidInit(): void {
  if (mermaidReady) return;
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    themeVariables: {
      background: "#ffffff",
      mainBkg: "#fafafa",
      secondBkg: "#f1f5f9",
      tertiaryColor: "#f1f5f9",
      textColor: "#1a1a1a",
      primaryTextColor: "#1a1a1a",
      secondaryTextColor: "#1a1a1a",
      tertiaryTextColor: "#666666",
      primaryColor: "#fafafa",
      primaryBorderColor: "#1a1a1a",
      secondaryBorderColor: "#1a1a1a",
      tertiaryBorderColor: "#1a1a1a",
      lineColor: "#1a1a1a",
      noteBkgColor: "#fff7e6",
      noteTextColor: "#92400e",
      noteBorderColor: "#fcd9a3",
    },
    fontFamily: '"Open Sans", system-ui, -apple-system, sans-serif',
    flowchart: { htmlLabels: false },
  });
  mermaidReady = true;
}

const renderer = new marked.Renderer();
const origCode = renderer.code.bind(renderer);
renderer.code = function (token) {
  const lang = (token.lang || "").trim().toLowerCase();
  if (lang === "mermaid") {
    const b64 = utf8ToBase64(token.text);
    return `<div class="mermaid-pending" data-mermaid="pending" data-mermaid-source="${b64}"></div>`;
  }
  if (lang === "svg") {
    // Hand the raw SVG straight through; the outer DOMPurify pass below
    // sanitises it (drops <script>, event handlers, javascript: URLs).
    return token.text;
  }
  return origCode(token);
};

// Custom block tokenizer for raw `<svg>…</svg>` written directly into a
// message. CommonMark only recognises a fixed list of HTML block tags
// (no SVG), and `breaks: true` rewrites every newline inside the SVG
// into a `<br>`, so multi-line markup gets mangled.
const svgBlock = {
  name: "svgBlock",
  level: "block" as const,
  start(src: string) {
    const i = src.search(/<svg[\s>]/i);
    return i >= 0 ? i : undefined;
  },
  tokenizer(src: string) {
    const m = /^<svg\b[\s\S]*?<\/svg>\s*/i.exec(src);
    if (!m) return undefined;
    return {
      type: "svgBlock",
      raw: m[0],
      text: m[0].trimEnd(),
    };
  },
  renderer(token: { text: string }) {
    return token.text;
  },
};

marked.use({ renderer, extensions: [svgBlock], gfm: true, breaks: true });

const svgCache = new Map<string, string>();
let renderId = 0;

export function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false }) as string;
  return DOMPurify.sanitize(html, {
    ADD_ATTR: [
      "target",
      "rel",
      "xmlns",
      "xmlns:xlink",
      "xlink:href",
      "preserveAspectRatio",
    ],
  });
}

export async function hydrateMermaid(root: ParentNode | null): Promise<void> {
  if (!root) return;
  ensureMermaidInit();
  const pending = Array.from(
    root.querySelectorAll<HTMLDivElement>('div[data-mermaid="pending"]'),
  );
  if (pending.length === 0) return;

  await Promise.all(
    pending.map(async (el) => {
      const b64 = el.getAttribute("data-mermaid-source") || "";
      let source: string;
      try {
        source = base64ToUtf8(b64);
      } catch {
        mountError(el, "could not decode mermaid source");
        return;
      }
      let svg = svgCache.get(source);
      if (!svg) {
        try {
          const id = `mermaid-${++renderId}`;
          const result = await mermaid.render(id, source);
          svg = result.svg;
          svgCache.set(source, svg);
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : String(err);
          mountError(el, msg);
          return;
        }
      }
      if (!el.isConnected) return;
      const wrap = document.createElement("div");
      wrap.className = "mermaid";
      wrap.innerHTML = svg;
      el.replaceWith(wrap);
    }),
  );
}

function mountError(el: HTMLElement, message: string): void {
  if (!el.isConnected) return;
  const pre = document.createElement("pre");
  pre.className = "mermaid-error";
  pre.textContent = `mermaid render error:\n${message}`;
  el.replaceWith(pre);
}

function utf8ToBase64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

function base64ToUtf8(b64: string): string {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}
