import { useEffect, useRef } from "preact/hooks";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
}

export function Composer({ value, onChange, onSend, onStop, busy }: Props) {
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(160, Math.max(36, ta.scrollHeight)) + "px";
  }, [value]);

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!busy && value.trim()) onSend();
    }
  }

  return (
    <div class="composer">
      <textarea
        class="composer-input"
        ref={taRef}
        value={value}
        rows={2}
        placeholder="Type a message…  (Enter to send, Shift+Enter for newline)"
        onInput={(e) => onChange((e.target as HTMLTextAreaElement).value)}
        onKeyDown={onKeyDown}
      />
      {/* Single button that morphs Send ↔ Stop in place. Keeping the same
          DOM node avoids focus jitter when the state flips mid-keystroke. */}
      <button
        class={`send-btn${busy ? " stop" : ""}`}
        type="button"
        disabled={!busy && !value.trim()}
        title={busy ? "Stop the in-flight turn" : "Send"}
        onClick={busy ? onStop : onSend}
      >
        {busy ? "Stop" : "Send"}
      </button>
    </div>
  );
}
