/** @jsxImportSource react */
// Shared flow-node chrome — used by every step type.
// **Real React** — see file-top pragma. ReactFlow drives node
// rendering, which means nodes must be real React components.
//
// Engineering vocabulary:
//
//   ┌─ title bar ──────────────────────────────┐
//   │ [icon]  STEP_ID                  TYPE    │
//   ├──────────────────────────────────────────┤
//   │   Label text                             │
//   │   [badge] [badge] [badge]                │
//   │   ▸ Input  : Request Form                │
//   │   Skips review if amount < $1k           │
//   └──────────────────────────────────────────┘

import { Handle, Position } from "@xyflow/react";

import type { FlowStep } from "../../lib/flow-types";
import { NamedIcon, StepIcon } from "./icons";

export interface NodeData extends Record<string, unknown> {
  step: FlowStep;
}

interface Props {
  data: NodeData;
  selected?: boolean;
}

// CSS in user-supplied styles arrives with kebab-case keys (matching
// the backend safe-list). React's `style` prop wants camelCase, so we
// translate at the boundary.
function kebabToCamelStyle(
  style: Record<string, string> | undefined,
): React.CSSProperties | undefined {
  if (!style) return undefined;
  const out: Record<string, string> = {};
  for (const k of Object.keys(style)) {
    const camel = k.replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
    out[camel] = style[k];
  }
  return out as React.CSSProperties;
}

const TYPE_LABEL: Record<FlowStep["type"], string> = {
  trigger: "TRIGGER",
  activity: "ACTIVITY",
  decision: "DECISION",
  artifact: "ARTIFACT",
  end: "END",
};

export function BaseNode({ data, selected }: Props) {
  const step = data.step;
  const tone = step.tone ?? "default";

  const nodeStyle = kebabToCamelStyle(step.style);
  const titleStyle = kebabToCamelStyle(step.title_style);

  return (
    <div
      className={`flow-node flow-node--${step.type} tone-${tone}${selected ? " is-selected" : ""}`}
      data-tone={tone}
      data-type={step.type}
      style={nodeStyle}
    >
      {/* Single hidden source + single hidden target handle. The
          FloatingEdge component computes its actual endpoints from
          node geometry (bounding-box intersection), so the Position
          values here are mostly nominal — they just need to exist for
          ReactFlow to attach edges to. CSS hides the dots entirely
          (the floating edge meets the node boundary directly). */}
      <Handle type="target" position={Position.Top} id="t" className="flow-handle--invisible" />

      <header className="flow-node__title" style={titleStyle}>
        <span className="flow-node__title-icon">
          <StepIcon step={step} size={14} />
        </span>
        <span className="flow-node__title-id" title={step.id}>{step.id}</span>
        <span className="flow-node__title-type">{TYPE_LABEL[step.type]}</span>
      </header>

      <div className="flow-node__body">
        <div className="flow-node__label">{step.label}</div>

        {step.badges && step.badges.length > 0 && (
          <div className="flow-node__badges">
            {step.badges.map((b, i) => (
              <span key={i} className={`flow-node__badge tone-${b.tone}`}>{b.label}</span>
            ))}
          </div>
        )}

        {((step.roles && step.roles.length > 0) ||
          (step.artifacts_in && step.artifacts_in.length > 0) ||
          (step.artifacts_out && step.artifacts_out.length > 0)) && (
          <div className="flow-node__inferred-meta">
            {step.roles && step.roles.length > 0 && (
              <div className="flow-node__meta-row">
                <span className="flow-node__meta-key">
                  <NamedIcon name="users" size={11} /> Roles
                </span>
                <span className="flow-node__meta-value">{step.roles.join(", ")}</span>
              </div>
            )}
            {step.artifacts_in && step.artifacts_in.length > 0 && (
              <div className="flow-node__meta-row">
                <span className="flow-node__meta-key">
                  <NamedIcon name="inbox" size={11} /> In
                </span>
                <span className="flow-node__meta-value">{step.artifacts_in.join(", ")}</span>
              </div>
            )}
            {step.artifacts_out && step.artifacts_out.length > 0 && (
              <div className="flow-node__meta-row">
                <span className="flow-node__meta-key">
                  <NamedIcon name="send" size={11} /> Out
                </span>
                <span className="flow-node__meta-value">{step.artifacts_out.join(", ")}</span>
              </div>
            )}
          </div>
        )}

        {step.meta && step.meta.length > 0 && (
          <div className="flow-node__meta">
            {step.meta.map((r, i) => (
              <div key={i} className="flow-node__meta-row">
                <span className="flow-node__meta-key">{r.key}</span>
                <span className="flow-node__meta-value">{r.value}</span>
              </div>
            ))}
          </div>
        )}

        {step.note && <div className="flow-node__note">{step.note}</div>}
      </div>

      <Handle type="source" position={Position.Bottom} id="b" className="flow-handle--invisible" />
    </div>
  );
}
