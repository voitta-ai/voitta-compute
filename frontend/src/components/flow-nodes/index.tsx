/** @jsxImportSource react */
// Node-type registry for ReactFlow.
//
// All non-decision steps use BaseNode. Decision steps dispatch on
// `step.shape`: 'rect' → BaseNode (default), 'port' → DecisionPortNode,
// 'diamond' → DecisionDiamondNode, 'junction' → DecisionJunctionNode.
//
// Why dispatch at the component level instead of declaring four
// different ReactFlow node type keys: the wire format keeps `type =
// "decision"` for all four shapes (semantic type), and adds `shape`
// as a sub-axis. That keeps the JSON definition stable across
// re-skins (changing a shape doesn't change the node type) and means
// the layout engine just sees "decision" for sizing.

import type { FlowStep } from "../../lib/flow-types";
import { BaseNode } from "./BaseNode";
import { DecisionDiamondNode } from "./DecisionDiamondNode";
import { DecisionJunctionNode } from "./DecisionJunctionNode";
import { DecisionPortNode } from "./DecisionPortNode";

function DecisionDispatcher(props: { data: { step: FlowStep }; selected?: boolean }) {
  const shape = props.data.step.shape ?? "rect";
  if (shape === "port") return <DecisionPortNode {...props} />;
  if (shape === "diamond") return <DecisionDiamondNode {...props} />;
  if (shape === "junction") return <DecisionJunctionNode {...props} />;
  return <BaseNode {...props} />;
}

export const flowNodeTypes: Record<string, React.ComponentType<any>> = {
  trigger: BaseNode,
  activity: BaseNode,
  decision: DecisionDispatcher,
  artifact: BaseNode,
  end: BaseNode,
};
