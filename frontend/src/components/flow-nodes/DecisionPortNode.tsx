/** @jsxImportSource react */
// Multi-port decision node — schematic-style.
//
// Each branch is a labeled row in the node body with a dedicated
// output Handle (id=`port-${index}`) on the right side. Edges
// attached to each port are explicit FixedEdges (sourceHandle bound
// to "port-N"), NOT FloatingEdges.
//
// Enhancement vs. earlier version: each port row uses
// `useNodeConnections` to find the edge attached to its handle and
// resolve the TARGET node's label, which it renders inline next to
// the branch label. So instead of just "Critical ●" you see
// "Critical → critical_handler ●" — the port row self-documents
// where the branch goes.
//
//   ┌─ ROUTE          DECISION ─┐
//   │ Route by Priority         │
//   ├───────────────────────────┤
//   │  Critical  → crit_handler ●───►
//   │  High      → high_handler ●───►
//   │  Medium    → med_handler  ●───►
//   │  Low       → low_handler  ●───►
//   │  Deferred  → defer_handler●───►
//   └───────────────────────────┘

import {
  Handle,
  Position,
  useNodeConnections,
  useReactFlow,
} from "@xyflow/react";

import type { FlowStep } from "../../lib/flow-types";
import { StepIcon } from "./icons";

interface Props {
  data: { step: FlowStep };
  selected?: boolean;
}

// One port row, isolated as a child component so each row can call
// `useNodeConnections` (hooks must be top-level — calling them in a
// `.map(...)` callback at the parent would violate rules-of-hooks).
function PortRow({
  index,
  label,
}: {
  index: number;
  label: string;
}) {
  const handleId = `port-${index}`;
  const connections = useNodeConnections({
    handleType: "source",
    handleId,
  });
  const rf = useReactFlow();

  // First connection's target is "where this branch goes". Multiple
  // edges from the same port is unusual but supported — we just show
  // the first.
  let destinationLabel = "";
  if (connections.length > 0) {
    const targetId = connections[0].target;
    const targetNode = rf.getNode(targetId);
    if (targetNode && targetNode.data && (targetNode.data as any).step) {
      const step = (targetNode.data as any).step as FlowStep;
      destinationLabel = step.label || step.id;
    }
  }

  // Truncate long destinations so the row doesn't bust the node width.
  const truncated =
    destinationLabel.length > 16
      ? destinationLabel.slice(0, 14) + "…"
      : destinationLabel;

  return (
    <div className="flow-decision-port">
      <span className="flow-decision-port__label">{label}</span>
      {truncated && (
        <span className="flow-decision-port__dest" title={destinationLabel}>
          → {truncated}
        </span>
      )}
      <span className="flow-decision-port__dot" />
      <Handle
        type="source"
        position={Position.Right}
        id={handleId}
        className="flow-decision-port__handle"
      />
    </div>
  );
}

export function DecisionPortNode({ data, selected }: Props) {
  const step = data.step;
  const tone = step.tone ?? "default";
  const branches = step.conditions ?? [];

  return (
    <div
      className={`flow-node flow-node--decision flow-node--decision-port tone-${tone}${selected ? " is-selected" : ""}`}
      data-tone={tone}
      data-type="decision"
      data-shape="port"
    >
      <Handle type="target" position={Position.Left} id="t" className="flow-handle--invisible" />

      <header className="flow-node__title">
        <span className="flow-node__title-icon">
          <StepIcon step={step} size={14} />
        </span>
        <span className="flow-node__title-id" title={step.id}>{step.id}</span>
        <span className="flow-node__title-type">◆ DECISION</span>
      </header>

      <div className="flow-node__body">
        <div className="flow-node__label">{step.label}</div>

        <div className="flow-decision-ports">
          {branches.map((b, i) => (
            <PortRow key={i} index={i} label={b.label} />
          ))}
        </div>
      </div>
    </div>
  );
}
