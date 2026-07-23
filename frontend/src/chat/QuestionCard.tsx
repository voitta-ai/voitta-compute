// QuestionCard: renders an AskUserQuestion call from the Claude brain.
//
// The backend bridges the engine's AskUserQuestion tool over Chainlit's
// *element* ask channel: a CustomElement named "AskUserQuestion" carries the
// questions JSON in its props, and the ask's ack callback accepts an arbitrary
// reply dict — {answers, annotations?} on submit, {response} for a freeform
// composer reply (wired in ChatPane), {cancelled: true} on cancel.
//
// UI contract mirrors real Claude Code:
//  - 1–4 questions → tab chips by `header` (✓ once answered); single question
//    renders without the tab row.
//  - 2–4 options; radio rows for single-select, checkboxes for multiSelect.
//  - "(Recommended)" suffix on a label renders as a badge, not literal text.
//  - "Other…" expands a free-text input; the typed text IS the answer value
//    (never the word "Other"), on multi-select it joins the selected labels.
//  - Options may carry a markdown `preview` (single-select only, per the tool
//    spec) → side-by-side layout, preview follows the focused option; an
//    optional note on the selection travels back as `annotations`.
// Answers are keyed by the full question text — the wire contract.

import { useMemo, useState } from "react";
import type { IAsk } from "@chainlit/react-client";
import Markdown from "./Markdown";

interface QOption {
  label: string;
  description?: string;
  preview?: string;
}

interface Question {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options?: QOption[];
}

interface QState {
  selected: string[];
  otherOn: boolean;
  other: string;
  notes: string;
  focus: number; // option index the preview pane follows (-1 = Other row)
}

interface Props {
  ask: IAsk;
  questions: Question[];
}

const OTHER = -1;

function freshState(): QState {
  return { selected: [], otherOn: false, other: "", notes: "", focus: 0 };
}

// "Deploy now (Recommended)" → { text: "Deploy now", recommended: true }
function splitRecommended(label: string): { text: string; recommended: boolean } {
  const m = label.match(/^(.*?)\s*\(recommended\)\s*$/i);
  return m ? { text: m[1], recommended: true } : { text: label, recommended: false };
}

function isAnswered(q: Question, st: QState): boolean {
  if (st.otherOn) return st.other.trim().length > 0 || st.selected.length > 0;
  return st.selected.length > 0;
}

export default function QuestionCard({ ask, questions }: Props) {
  const [tab, setTab] = useState(0);
  const [states, setStates] = useState<QState[]>(() => questions.map(freshState));

  // Defensive: the model occasionally sends fewer/more questions than the
  // state array if props change under us (re-mount safety).
  const qs = useMemo(() => questions.filter((q) => q && q.question), [questions]);
  if (qs.length === 0) return null;

  const active = Math.min(tab, qs.length - 1);
  const q = qs[active];
  const st = states[active] ?? freshState();

  function patch(idx: number, upd: Partial<QState>) {
    setStates((prev) => {
      const next = prev.slice();
      next[idx] = { ...(next[idx] ?? freshState()), ...upd };
      return next;
    });
  }

  // Radio select auto-advances to the next unanswered tab (CLI behavior).
  function pick(optIdx: number, label: string) {
    if (q.multiSelect) {
      const on = st.selected.includes(label);
      patch(active, {
        selected: on ? st.selected.filter((l) => l !== label) : [...st.selected, label],
        focus: optIdx,
      });
      return;
    }
    patch(active, { selected: [label], otherOn: false, focus: optIdx });
    const nextIdx = qs.findIndex(
      (qq, i) => i !== active && !isAnswered(qq, states[i] ?? freshState()),
    );
    if (nextIdx >= 0) setTab(nextIdx);
  }

  function toggleOther() {
    if (q.multiSelect) {
      patch(active, { otherOn: !st.otherOn, focus: OTHER });
    } else {
      patch(active, { otherOn: !st.otherOn, selected: [], focus: OTHER });
    }
  }

  const allAnswered = qs.every((qq, i) => isAnswered(qq, states[i] ?? freshState()));

  function submit() {
    if (!allAnswered) return;
    const answers: Record<string, string | string[]> = {};
    const annotations: Record<string, { notes: string; preview?: string }> = {};
    qs.forEach((qq, i) => {
      const s = states[i] ?? freshState();
      const typed = s.otherOn ? s.other.trim() : "";
      if (qq.multiSelect) {
        const vals = [...s.selected];
        if (typed) vals.push(typed);
        answers[qq.question] = vals;
      } else {
        answers[qq.question] = typed || s.selected[0] || "";
      }
      if (s.notes.trim()) {
        const sel = (qq.options ?? []).find((o) => s.selected.includes(o.label));
        annotations[qq.question] = {
          notes: s.notes.trim(),
          ...(sel?.preview ? { preview: sel.preview } : {}),
        };
      }
    });
    const reply: Record<string, unknown> = { answers };
    if (Object.keys(annotations).length) reply.annotations = annotations;
    ask.callback(reply as never);
  }

  function cancel() {
    ask.callback({ cancelled: true } as never);
  }

  const options = q.options ?? [];
  const hasPreviews = !q.multiSelect && options.some((o) => o.preview);
  const focused = st.focus === OTHER ? undefined : options[st.focus] ?? options[0];
  const showNotes = !q.multiSelect && st.selected.length > 0;

  return (
    <div className="qcard" role="form" aria-label="Claude is asking a question">
      {qs.length > 1 && (
        <div className="qcard-tabs" role="tablist">
          {qs.map((qq, i) => {
            const done = isAnswered(qq, states[i] ?? freshState());
            return (
              <button
                key={i}
                type="button"
                role="tab"
                aria-selected={i === active}
                className={`qcard-tab${i === active ? " is-active" : ""}${done ? " is-done" : ""}`}
                onClick={() => setTab(i)}
              >
                {done ? "✓ " : ""}
                {qq.header || `Q${i + 1}`}
              </button>
            );
          })}
        </div>
      )}

      <div className="qcard-question">{q.question}</div>

      <div className={`qcard-body${hasPreviews ? " has-preview" : ""}`}>
        <div className="qcard-options" role={q.multiSelect ? "group" : "radiogroup"}>
          {options.map((opt, i) => {
            const { text, recommended } = splitRecommended(opt.label);
            const checked = st.selected.includes(opt.label);
            return (
              <label
                key={i}
                className={`qcard-option${checked ? " is-selected" : ""}${
                  st.focus === i ? " is-focused" : ""
                }`}
                onMouseEnter={() => hasPreviews && patch(active, { focus: i })}
              >
                <input
                  type={q.multiSelect ? "checkbox" : "radio"}
                  name={`qcard-q${active}`}
                  checked={checked}
                  onChange={() => pick(i, opt.label)}
                />
                <span className="qcard-opt-text">
                  <span className="qcard-opt-label">
                    {text}
                    {recommended && <span className="qcard-badge">Recommended</span>}
                  </span>
                  {opt.description && (
                    <span className="qcard-opt-desc">{opt.description}</span>
                  )}
                </span>
              </label>
            );
          })}

          <label
            className={`qcard-option qcard-other${st.otherOn ? " is-selected" : ""}`}
            onMouseEnter={() => hasPreviews && patch(active, { focus: OTHER })}
          >
            <input
              type={q.multiSelect ? "checkbox" : "radio"}
              name={`qcard-q${active}`}
              checked={st.otherOn}
              onChange={toggleOther}
            />
            <span className="qcard-opt-text">
              <span className="qcard-opt-label">Other…</span>
            </span>
          </label>
          {st.otherOn && (
            <input
              type="text"
              className="qcard-other-input"
              placeholder="Type your answer…"
              value={st.other}
              autoFocus
              onChange={(e) => patch(active, { other: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter" && allAnswered) submit();
              }}
            />
          )}

          {showNotes && (
            <input
              type="text"
              className="qcard-notes"
              placeholder="Add a note (optional)…"
              value={st.notes}
              onChange={(e) => patch(active, { notes: e.target.value })}
            />
          )}
        </div>

        {hasPreviews && (
          <div className="qcard-preview" aria-label="Option preview">
            {focused?.preview ? (
              <Markdown text={focused.preview} />
            ) : (
              <div className="qcard-preview-empty">No preview</div>
            )}
          </div>
        )}
      </div>

      <div className="qcard-actions">
        <button type="button" className="qcard-btn" onClick={cancel}>
          Cancel
        </button>
        <button
          type="button"
          className="qcard-btn qcard-submit"
          disabled={!allAnswered}
          onClick={submit}
        >
          {qs.length > 1
            ? `Submit (${qs.filter((qq, i) => isAnswered(qq, states[i] ?? freshState())).length}/${qs.length})`
            : "Submit"}
        </button>
      </div>
      <div className="qcard-hint">…or just type a reply below</div>
    </div>
  );
}
