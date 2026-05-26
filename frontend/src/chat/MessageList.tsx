// Scrolling message list. Flattens the nested step tree by
// ``createdAt`` (Chainlit's tree is hard to render naively) and
// auto-scrolls to the bottom on every update.

import { useEffect, useMemo, useRef } from "react";
import { type IMessageElement, type IStep } from "@chainlit/react-client";
import StepView from "./StepView";

interface Props {
  steps: IStep[];
  elements: IMessageElement[];
  backendOrigin: string;
  emptyHint: string;
}

export default function MessageList({ steps, elements, backendOrigin, emptyHint }: Props) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const flat = useMemo(() => flattenSteps(steps), [steps]);

  // Group inline elements by parent step id for O(1) lookup in StepView.
  const elementsByStep = useMemo(() => {
    const map = new Map<string, IMessageElement[]>();
    for (const el of elements) {
      if (!el.forId) continue;
      const arr = map.get(el.forId) ?? [];
      arr.push(el);
      map.set(el.forId, arr);
    }
    return map;
  }, [elements]);

  useEffect(() => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [flat, elements]);

  return (
    <div className="messages" ref={scrollerRef}>
      {flat.length === 0 && (
        <div className="empty">
          <div className="badge">●</div>
          <div>{emptyHint}</div>
        </div>
      )}
      {flat.map((s) => (
        <StepView
          key={s.id}
          step={s}
          elements={elementsByStep.get(s.id) ?? []}
          backendOrigin={backendOrigin}
        />
      ))}
    </div>
  );
}

function flattenSteps(steps: IStep[]): IStep[] {
  const out: IStep[] = [];
  const walk = (list: IStep[]) => {
    for (const s of list) {
      out.push(s);
      if (s.steps?.length) walk(s.steps);
    }
  };
  walk(steps);
  return out.sort((a, b) => {
    const ta = a.createdAt ? Date.parse(a.createdAt as unknown as string) : 0;
    const tb = b.createdAt ? Date.parse(b.createdAt as unknown as string) : 0;
    return ta - tb;
  });
}
