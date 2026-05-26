// Composer: textarea + toolbar (attach, attachment chips, send/stop).
// Supports drag-drop, paste-from-clipboard, and the file picker.
// Class names track styles/components/composer.css.

import { useEffect, useRef, useState } from "react";
import type { ImageAttachment } from "../lib/image-attach";
import { extractImageFiles } from "../lib/image-attach";

const ICON_PLUS = (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path
      d="M8 3v10M3 8h10"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      fill="none"
    />
  </svg>
);
const ICON_SEND = (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path
      d="M8 14V3M3 8l5-5 5 5"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      fill="none"
    />
  </svg>
);
// Stop glyph: a filled rounded square that fills ~50% of the button
// (matches the visual weight of the send arrow it replaces). We
// intentionally override the parent ``.ctb-icon svg`` 18px sizing
// with a slightly larger 16px so the square reads as the recognisable
// "stop" affordance rather than a tiny dot.
const ICON_STOP = (
  <svg
    viewBox="0 0 24 24"
    width="16"
    height="16"
    aria-hidden="true"
    style={{ display: "block" }}
  >
    <rect x={5} y={5} width={14} height={14} rx={2} fill="currentColor" />
  </svg>
);

interface Props {
  busy: boolean;
  attachments: ImageAttachment[];
  onAttach: (files: File[]) => void;
  onRemoveAttachment: (index: number) => void;
  onSend: (text: string) => void;
  onStop: () => void;
}

export default function Composer({
  busy,
  attachments,
  onAttach,
  onRemoveAttachment,
  onSend,
  onStop,
}: Props) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState("");
  const [dragOver, setDragOver] = useState(false);

  // Auto-resize the textarea height to content, clamped to 36–160px.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(160, Math.max(36, ta.scrollHeight)) + "px";
  }, [value]);

  function submit() {
    if (busy) return;
    if (!value.trim() && attachments.length === 0) return;
    onSend(value);
    setValue("");
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const files = extractImageFiles(e.clipboardData?.items ?? null);
    if (!files.length) return;
    e.preventDefault();
    onAttach(files);
  }

  // Drag tracking uses a depth counter to survive `dragleave` fired
  // when the cursor crosses into a descendant element.
  const dragDepth = useRef(0);
  function isImageDrag(e: React.DragEvent): boolean {
    const types = e.dataTransfer?.types;
    if (!types) return false;
    for (let i = 0; i < types.length; i++) {
      if (types[i] === "Files") return true;
    }
    return false;
  }
  function onDragEnter(e: React.DragEvent) {
    if (!isImageDrag(e)) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragOver(true);
  }
  function onDragOver(e: React.DragEvent) {
    if (!isImageDrag(e)) return;
    e.preventDefault();
  }
  function onDragLeave(e: React.DragEvent) {
    if (!isImageDrag(e)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragOver(false);
  }
  function onDrop(e: React.DragEvent) {
    if (!isImageDrag(e)) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDragOver(false);
    const files = extractImageFiles(e.dataTransfer?.items ?? null);
    if (files.length) onAttach(files);
  }

  function onPlusClick() {
    fileInputRef.current?.click();
  }
  function onFilePicked(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files ? Array.from(e.target.files) : [];
    if (files.length) onAttach(files);
    // reset so picking the same file twice in a row still fires
    e.target.value = "";
  }

  const canSend = !busy && (value.trim().length > 0 || attachments.length > 0);

  return (
    <div
      className={`composer${dragOver ? " is-dragover" : ""}`}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <textarea
        ref={taRef}
        className="composer-input"
        rows={2}
        placeholder="Type a message…  (Enter to send, Shift+Enter for newline)"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        onPaste={handlePaste}
      />
      <div className="composer-toolbar">
        <button
          className="ctb-icon"
          type="button"
          title="Attach image"
          aria-label="Attach image"
          onClick={onPlusClick}
          disabled={busy}
        >
          {ICON_PLUS}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          style={{ display: "none" }}
          onChange={onFilePicked}
        />
        <div className="ctb-attachments">
          {attachments.map((att, i) => (
            <div className="ctb-chip" key={`${att.dataUrl.slice(0, 32)}-${i}`}>
              <img src={att.dataUrl} alt="" />
              <button
                className="ctb-chip-x"
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
          className={`ctb-icon ctb-send${busy ? " is-stop" : ""}`}
          type="button"
          disabled={!busy && !canSend}
          title={busy ? "Stop the in-flight turn" : "Send"}
          aria-label={busy ? "Stop" : "Send"}
          onClick={busy ? onStop : submit}
        >
          {busy ? ICON_STOP : ICON_SEND}
        </button>
      </div>
      {dragOver && (
        <div className="composer-drop-overlay" aria-hidden="true">
          Drop image to attach
        </div>
      )}
    </div>
  );
}
