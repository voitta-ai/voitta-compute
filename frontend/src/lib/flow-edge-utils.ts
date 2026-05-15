/** @jsxImportSource react */
// Floating-edge geometry helpers.
//
// Ported from the canonical ReactFlow example at
// `examples/react/src/examples/FloatingEdges/utils.ts` and
// `examples/react/src/examples/EasyConnect/utils.tsx`. The output is
// what a custom FloatingEdge component needs to call any of the
// `getXPath` helpers (getBezierPath, getSmoothStepPath, etc.):
//
//   getEdgeParams(source, target) → {
//     sx, sy, tx, ty,             // source / target x,y on the node boundary
//     sourcePos, targetPos,       // Position.{Top|Right|Bottom|Left}
//   }
//
// We trade explicit Handles (4 per node, one per side) for a single
// invisible source + single invisible target handle and let the math
// pick which side of the node the edge meets.

import { type InternalNode, Position, type XYPosition } from "@xyflow/react";

// Returns the point where a line from the centre of `intersection` to
// the centre of `target` hits `intersection`'s axis-aligned bounding
// box.
function getNodeIntersection(intersection: InternalNode, target: InternalNode): XYPosition {
  const intMeasured = intersection.measured ?? { width: 0, height: 0 };
  const intW = intMeasured.width ?? 0;
  const intH = intMeasured.height ?? 0;
  const intAbs = intersection.internals.positionAbsolute;

  const targetMeasured = target.measured ?? { width: 0, height: 0 };
  const targetW = targetMeasured.width ?? 0;
  const targetH = targetMeasured.height ?? 0;
  const targetAbs = target.internals.positionAbsolute;

  const w = intW / 2;
  const h = intH / 2;

  const x2 = intAbs.x + w;
  const y2 = intAbs.y + h;
  const x1 = targetAbs.x + targetW / 2;
  const y1 = targetAbs.y + targetH / 2;

  // Math from the upstream FloatingEdges example: solves for the
  // intersection of the centre-to-centre line with the rectangle.
  const xx1 = (x1 - x2) / (2 * w) - (y1 - y2) / (2 * h);
  const yy1 = (x1 - x2) / (2 * w) + (y1 - y2) / (2 * h);
  const a = 1 / (Math.abs(xx1) + Math.abs(yy1) || 1);
  const xx3 = a * xx1;
  const yy3 = a * yy1;
  const x = w * (xx3 + yy3) + x2;
  const y = h * (-xx3 + yy3) + y2;

  return { x, y };
}

// Given a node and an intersection point on its border, which Position
// is the point closest to (top / right / bottom / left)?
function getEdgePosition(node: InternalNode, intersectionPoint: XYPosition): Position {
  const measured = node.measured ?? { width: 0, height: 0 };
  const w = measured.width ?? 0;
  const h = measured.height ?? 0;
  const abs = node.internals.positionAbsolute;
  const nx = Math.round(abs.x);
  const ny = Math.round(abs.y);
  const px = Math.round(intersectionPoint.x);
  const py = Math.round(intersectionPoint.y);

  if (px <= nx + 1) return Position.Left;
  if (px >= nx + w - 1) return Position.Right;
  if (py <= ny + 1) return Position.Top;
  if (py >= ny + h - 1) return Position.Bottom;
  return Position.Top;
}

export interface EdgeParams {
  sx: number;
  sy: number;
  tx: number;
  ty: number;
  sourcePos: Position;
  targetPos: Position;
}

/** Compute the floating-edge endpoint params for an edge from
 *  `source` to `target`. Returns coordinates IN ABSOLUTE FLOW SPACE
 *  (not screen pixels — ReactFlow handles the viewport transform).
 */
export function getEdgeParams(source: InternalNode, target: InternalNode): EdgeParams {
  const sourceIntersection = getNodeIntersection(source, target);
  const targetIntersection = getNodeIntersection(target, source);
  const sourcePos = getEdgePosition(source, sourceIntersection);
  const targetPos = getEdgePosition(target, targetIntersection);
  return {
    sx: sourceIntersection.x,
    sy: sourceIntersection.y,
    tx: targetIntersection.x,
    ty: targetIntersection.y,
    sourcePos,
    targetPos,
  };
}
