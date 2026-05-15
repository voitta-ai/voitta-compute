/** @jsxImportSource react */
// Classic BPMN-style decision — diamond / rhombus shape.
//
// Implementation note: we use `clip-path: polygon(...)` to carve a
// diamond out of an axis-aligned rectangle. The previous version used
// `transform: rotate(45deg)` on the container, which broke edge
// routing: ReactFlow measures the node's UNROTATED bounding box
// while the user sees the ROTATED bounding box (sqrt(2)× larger).
// Edges aimed at points INSIDE the visible diamond. clip-path keeps
// the bounding box and visible shape identical, so handles at the
// top/right/bottom/left midpoints sit exactly at the diamond's tips.
//
//          ┌──╲
//          │   ╲     ← bounding box (clip-pathed)
//        ╱─┘    ╲
//       ╱  ◆     ●   ← right handle at midpoint = diamond's right tip
//        ╲ ?    ╱
//         ╲    ╱
//          ╲  ╱
//           ╲╱

import { Handle, Position } from "@xyflow/react";

import type { FlowStep } from "../../lib/flow-types";

interface Props {
  data: { step: FlowStep };
  selected?: boolean;
}

export function DecisionDiamondNode({ data, selected }: Props) {
  const step = data.step;
  const tone = step.tone ?? "default";

  return (
    <div
      className={`flow-node--decision-diamond tone-${tone}${selected ? " is-selected" : ""}`}
      data-tone={tone}
      data-type="decision"
      data-shape="diamond"
    >
      {/* Single hidden source + single hidden target handle. The
          FloatingEdge computes endpoints from the diamond's bounding
          box — edges naturally meet at the four tips because the
          intersection algorithm finds the closest border point. */}
      <Handle type="target" position={Position.Top} id="t" className="flow-handle--invisible" />
      <Handle type="source" position={Position.Bottom} id="b" className="flow-handle--invisible" />

      <div className="flow-node--decision-diamond__inner">
        <div className="flow-node--decision-diamond__type">◆ DECISION</div>
        <div className="flow-node--decision-diamond__label">{step.label}</div>
        <div className="flow-node--decision-diamond__id">{step.id}</div>
      </div>
    </div>
  );
}
