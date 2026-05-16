/** @jsxImportSource react */
// Edge-type registry for ReactFlow.
//
// Three edge kinds:
//   • orthogonal — true rectilinear path through ELK-computed bend
//     points. Used when ELK ran the layout AND returned bend points
//     for this edge. The "engineering schematic" default.
//   • floating  — endpoints computed from node geometry; smoothstep
//     path (ReactFlow's approximate orthogonal). Used when no bend
//     points are available (dagre layouts, or ELK with non-orthogonal
//     routing).
//   • fixed     — explicit source/target handle attach; smoothstep
//     path. Used by port-shape decision branches.

import { FixedEdge } from "./FixedEdge";
import { FloatingEdge } from "./FloatingEdge";
import { OrthogonalEdge } from "./OrthogonalEdge";

export const flowEdgeTypes = {
  orthogonal: OrthogonalEdge,
  floating: FloatingEdge,
  fixed: FixedEdge,
};
