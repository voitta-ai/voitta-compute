import { useEffect, useRef, useState } from "preact/hooks";

import type { ImageAttachment } from "../lib/image-attach";
import { extractImageFiles, resizeAndEncode } from "../lib/image-attach";
import { log } from "../lib/logger";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
  attachments: ImageAttachment[];
  onAttach: (files: File[]) => void;
  onRemoveAttachment: (index: number) => void;
}

// Inline SVGs used by the toolbar buttons. Kept inline so they pick up
// `currentColor` from --voitta-text and follow the active plugin theme.
const ICON_PLUS = (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path
      d="M8 3v10M3 8h10"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      fill="none"
    />
  </svg>
);
const ICON_SEND = (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path
      d="M8 14V3M3 8l5-5 5 5"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      fill="none"
    />
  </svg>
);
const ICON_STOP = (
  <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
    <rect x="2" y="2" width="12" height="12" rx="1" fill="currentColor" />
  </svg>
);

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  busy,
  attachments,
  onAttach,
  onRemoveAttachment,
}: Props) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  // Auto-resize the textarea height to content, clamped to a 36–160 px band.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(160, Math.max(36, ta.scrollHeight)) + "px";
  }, [value]);

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!busy && (value.trim() || attachments.length)) onSend();
    }
  }

  function handlePaste(e: ClipboardEvent) {
    const files = extractImageFiles(e.clipboardData?.items ?? null);
    if (!files.length) return;
    e.preventDefault();
    onAttach(files);
  }

  // Drag-drop on the composer surface itself. We use a ref counter to
  // survive `dragenter`/`dragleave` events fired for descendant nodes
  // (the browser fires leave on every child transition, which would
  // otherwise reset the highlight mid-drag).
  const dragDepth = useRef(0);
  function isImageDrag(e: DragEvent): boolean {
    const types = e.dataTransfer?.types;
    if (!types) return false;
    for (let i = 0; i < types.length; i++) {
      if (types[i] === "Files") return true;
    }
    return false;
  }
  function handleDragEnter(e: DragEvent) {
    if (!isImageDrag(e)) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragOver(true);
  }
  function handleDragOver(e: DragEvent) {
    if (!isImageDrag(e)) return;
    e.preventDefault();
  }
  function handleDragLeave(e: DragEvent) {
    if (!isImageDrag(e)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragOver(false);
  }
  function handleDrop(e: DragEvent) {
    if (!isImageDrag(e)) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDragOver(false);
    const files = extractImageFiles(e.dataTransfer?.items ?? null);
    if (files.length) onAttach(files);
  }

  function handlePlusClick() {
    fileInputRef.current?.click();
  }
  function handleFilePicked(e: Event) {
    const input = e.target as HTMLInputElement;
    const files = input.files ? Array.from(input.files) : [];
    if (files.length) onAttach(files);
    // Reset so picking the same file twice in a row still fires onchange.
    input.value = "";
  }

  const canSend = !busy && (value.trim().length > 0 || attachments.length > 0);

  return (
    <div
      class={`composer${dragOver ? " is-dragover" : ""}`}
      onDragEnter={handleDragEnter as any}
      onDragOver={handleDragOver as any}
      onDragLeave={handleDragLeave as any}
      onDrop={handleDrop as any}
    >
      <textarea
        class="composer-input"
        ref={taRef}
        value={value}
        rows={2}
        placeholder="Type a message…  (Enter to send, Shift+Enter for newline)"
        onInput={(e) => onChange((e.target as HTMLTextAreaElement).value)}
        onKeyDown={onKeyDown}
        onPaste={handlePaste as any}
      />
      <div class="composer-toolbar">
        <button
          class="ctb-icon"
          type="button"
          title="Attach image"
          aria-label="Attach image"
          onClick={handlePlusClick}
          disabled={busy}
        >
          {ICON_PLUS}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          style="display:none"
          onChange={handleFilePicked as any}
        />
        <div class="ctb-attachments">
          {attachments.map((att, i) => (
            <div class="ctb-chip" key={`${att.dataUrl.slice(0, 32)}-${i}`}>
              <img src={att.dataUrl} alt="" />
              <button
                class="ctb-chip-x"
                type="button"
                title="Remove"
                aria-label="Remove attachment"
                onClick={() => onRemoveAttachment(i)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
        <button
          class={`ctb-icon ctb-send${busy ? " is-stop" : ""}`}
          type="button"
          disabled={!busy && !canSend}
          title={busy ? "Stop the in-flight turn" : "Send"}
          aria-label={busy ? "Stop" : "Send"}
          onClick={busy ? onStop : onSend}
        >
          {busy ? ICON_STOP : ICON_SEND}
        </button>
      </div>
      {dragOver && (
        <div class="composer-drop-overlay" aria-hidden="true">
          Drop image to attach
        </div>
      )}
    </div>
  );
}

// Helper for parents: run the async resize pipeline over a batch of
// File objects, returning successfully-encoded attachments. Errors are
// logged but never thrown — a single bad file shouldn't block the rest.
export async function encodeFiles(files: File[]): Promise<ImageAttachment[]> {
  const out: ImageAttachment[] = [];
  for (const f of files) {
    try {
      out.push(await resizeAndEncode(f));
    } catch (err) {
      log.warn("composer", "image encode failed", {
        file: f.name,
        err: String(err),
      });
    }
  }
  return out;
}
