/** @jsxImportSource react */
// Fixed edge — uses explicit handle positions provided by the
// edge's sourceHandle / targetHandle. Used by port-shape decisions
// where each branch attaches to a specific labeled output row.
//
// Differs from FloatingEdge in just one place: source/target
// coordinates come from EdgeProps directly (ReactFlow has already
// resolved the handle positions for us) instead of computed via
// `getEdgeParams`. Everything else — rich DOM labels via
// EdgeLabelRenderer, tone class, path selection — is identical.

import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  getSmoothStepPath,
  getStraightPath,
  type EdgeProps,
} from "@xyflow/react";

interface FixedEdgeData {
  edgeType?: "smoothstep" | "step" | "straight" | "bezier";
  tone?: string;
  borderRadius?: number;
  offset?: number;
  stepPosition?: number;
}

export function FixedEdge({
  id,
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  markerEnd,
  markerStart,
  style,
  data,
  label,
  selected,
}: EdgeProps) {
  const d = data as FixedEdgeData | undefined;
  const edgeType = d?.edgeType ?? "smoothstep";

  const pathParams = {
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  };

  let path = "";
  let labelX = 0;
  let labelY = 0;
  if (edgeType === "step") {
    [path, labelX, labelY] = getSmoothStepPath({ ...pathParams, borderRadius: 0 });
  } else if (edgeType === "straight") {
    [path, labelX, labelY] = getStraightPath(pathParams);
  } else if (edgeType === "bezier") {
    [path, labelX, labelY] = getBezierPath(pathParams);
  } else {
    [path, labelX, labelY] = getSmoothStepPath({
      ...pathParams,
      borderRadius: d?.borderRadius,
      offset: d?.offset,
      stepPosition: d?.stepPosition,
    });
  }

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
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
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
