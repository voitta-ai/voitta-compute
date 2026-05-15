/** @jsxImportSource react */
// Tiny "routing junction" decision — a small labeled dot.
//
// Use when you have many branches and the LABELS on the outgoing
// edges are the story. The node itself is just a meeting point.
//
//          │
//          ●  Route by Priority
//        ╱╱│╲╲
//      ... edges with labels ...
//
// No title bar, no badges, no meta — just the dot + the label.
// Validation in FlowBuilder rejects roles/meta/note for this shape.

import { Handle, Position } from "@xyflow/react";

import type { FlowStep } from "../../lib/flow-types";

interface Props {
  data: { step: FlowStep };
  selected?: boolean;
}

export function DecisionJunctionNode({ data, selected }: Props) {
  const step = data.step;
  const tone = step.tone ?? "default";

  return (
    <div
      className={`flow-node--decision-junction tone-${tone}${selected ? " is-selected" : ""}`}
      data-tone={tone}
      data-type="decision"
      data-shape="junction"
    >
      <Handle type="target" position={Position.Top} id="t" className="flow-handle--invisible" />
      <Handle type="source" position={Position.Bottom} id="b" className="flow-handle--invisible" />

      <div className="flow-node--decision-junction__dot" />
      <div className="flow-node--decision-junction__label">{step.label}</div>
    </div>
  );
}
