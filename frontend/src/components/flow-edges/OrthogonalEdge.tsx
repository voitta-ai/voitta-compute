/** @jsxImportSource react */
// True orthogonal edge — renders a polyline through the bend points
// ELK computed during the layout pass. Used when
// `cfg.edge_routing === "orthogonal"` (the default) AND ELK actually
// returned bend points for this edge.
//
// Why this exists: ReactFlow's bundled `getSmoothStepPath` is a
// FAKE orthogonal — it's a 2-bend approximation that produces
// zig-zag detours when the source and target aren't aligned. ELK's
// orthogonal router does true rectilinear routing with N-bend
// polylines that avoid all crossings; we just need to render the
// path it computed.
//
// Each edge gets a single `<path d="M sx,sy L b1.x,b1.y L b2.x,b2.y …
// L tx,ty">` — no curves, no rounding, hard 90° corners. That's the
// schematic look.

import { BaseEdge, EdgeLabelRenderer, type EdgeProps } from "@xyflow/react";

interface OrthogonalEdgeData {
  tone?: string;
  bendPoints?: { x: number; y: number }[];
  sourcePoint?: { x: number; y: number };
  targetPoint?: { x: number; y: number };
}

// Build a polyline SVG path. Includes the source attach point, every
// bend point, and the target attach point. ELK gives us these in
// absolute layout coordinates which is what ReactFlow expects for
// edge paths.
function polylinePath(
  source: { x: number; y: number },
  bends: { x: number; y: number }[],
  target: { x: number; y: number },
): string {
  const parts = [`M ${source.x},${source.y}`];
  for (const b of bends) parts.push(`L ${b.x},${b.y}`);
  parts.push(`L ${target.x},${target.y}`);
  return parts.join(" ");
}

// Pick the midpoint of the longest segment as the label anchor —
// makes labels land on the longest straight run rather than at a
// corner where they'd compete with the bend visually.
function pickLabelAnchor(
  source: { x: number; y: number },
  bends: { x: number; y: number }[],
  target: { x: number; y: number },
): { x: number; y: number } {
  const pts = [source, ...bends, target];
  let best = { x: source.x, y: source.y };
  let bestLen = -1;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.hypot(dx, dy);
    if (len > bestLen) {
      bestLen = len;
      best = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    }
  }
  return best;
}

export function OrthogonalEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  markerStart,
  style,
  data,
  label,
  selected,
}: EdgeProps) {
  const d = data as OrthogonalEdgeData | undefined;
  const bends = d?.bendPoints ?? [];

  // Prefer ELK's computed attach points (where the edge meets each
  // node boundary). Fall back to ReactFlow's source/target props if
  // ELK didn't give us any — that happens for dagre layouts.
  const source = d?.sourcePoint ?? { x: sourceX, y: sourceY };
  const target = d?.targetPoint ?? { x: targetX, y: targetY };

  const path = polylinePath(source, bends, target);
  const anchor = pickLabelAnchor(source, bends, target);
  const tone = d?.tone ?? "default";

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        markerStart={markerStart}
        style={style}
        // @ts-ignore
        className={`flow-edge tone-${tone}${selected ? " is-selected" : ""}`}
      />
      {label && (
        <EdgeLabelRenderer>
          <div
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${anchor.x}px, ${anchor.y}px)`,
              pointerEvents: "all",
            }}
            className={`flow-edge-label tone-${tone}`}
            data-id={id}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
