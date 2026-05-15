/** @jsxImportSource react */
// Floating edge — computes its own endpoints from the source/target
// node geometry instead of relying on pre-placed handles. Replaces
// the 4-handle-per-node scaffolding for everything EXCEPT port-shape
// decisions (which still want explicit per-port handles).
//
// Picks the path function from `data.edgeType` (smoothstep / step /
// straight / bezier). Renders a rich label via <EdgeLabelRenderer>
// when `label` is set, so labels are real DOM with tone-aware styling
// rather than SVG <text> stuck in an awkward spot on the path.

import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  getSmoothStepPath,
  getStraightPath,
  useInternalNode,
  type EdgeProps,
} from "@xyflow/react";

import { getEdgeParams } from "../../lib/flow-edge-utils";

interface FloatingEdgeData {
  edgeType?: "smoothstep" | "step" | "straight" | "bezier";
  tone?: string;
  borderRadius?: number;
  offset?: number;
  stepPosition?: number;
}

export function FloatingEdge({
  id,
  source,
  target,
  sourceHandleId,
  targetHandleId,
  markerEnd,
  markerStart,
  style,
  data,
  label,
  selected,
}: EdgeProps) {
  const sourceNode = useInternalNode(source);
  const targetNode = useInternalNode(target);

  if (!sourceNode || !targetNode) {
    return null;
  }

  const { sx, sy, tx, ty, sourcePos, targetPos } = getEdgeParams(sourceNode, targetNode);
  const d = data as FloatingEdgeData | undefined;
  const edgeType = d?.edgeType ?? "smoothstep";

  const pathParams = {
    sourceX: sx,
    sourceY: sy,
    sourcePosition: sourcePos,
    targetX: tx,
    targetY: ty,
    targetPosition: targetPos,
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
        // We don't use BaseEdge's label props — we render our own DOM
        // label via EdgeLabelRenderer below so we get HTML/CSS tone
        // styling. SVG <text> is too limited.
        // @ts-ignore: passthrough only
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
