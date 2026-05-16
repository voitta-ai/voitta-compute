// Wire-format types for flow definitions.
//
// Shape matches `FlowBuilder.to_dict()` in
// backend/app/services/flow_builder.py — one source of truth, the
// Python side. Anything missing here that the backend may emit is a
// bug to fix in flow-types.ts, not silently swallowed.

export type FlowTone =
  | "default"
  | "info"
  | "success"
  | "warning"
  | "critical";

export type FlowEdgeStyle = "smoothstep" | "step" | "straight" | "bezier";
export type FlowDecisionShape = "rect" | "port" | "diamond" | "junction";
export type FlowBackground = "dots" | "lines" | "cross" | "none";
export type FlowLayoutEngine = "elk" | "dagre";
export type FlowDirection = "TB" | "LR" | "BT" | "RL";

export interface FlowIcon {
  // One of `name` (lucide-icons name in kebab-case) or `svg` (inline SVG).
  name?: string;
  svg?: string;
}

export interface FlowBadge {
  label: string;
  tone: FlowTone;
}

export interface FlowMetaRow {
  key: string;
  value: string;
}

export interface FlowStep {
  id: string;
  type: "trigger" | "activity" | "decision" | "artifact" | "end";
  label: string;
  description?: string;
  roles?: string[];
  artifacts_in?: string[];
  artifacts_out?: string[];

  // Decision-only: conditions paired with auto-created connections.
  conditions?: { label: string; target: string }[];
  // Decision-only: visual shape of the node.
  shape?: FlowDecisionShape;

  // Customization
  tone?: FlowTone;
  icon?: FlowIcon;
  badges?: FlowBadge[];
  meta?: FlowMetaRow[];
  note?: string;
  style?: Record<string, string>;
  title_style?: Record<string, string>;
  group?: string;
}

export type FlowMarker = "arrow" | "arrow-closed" | "none";
export type FlowColorMode = "light" | "dark" | "system" | "auto";

export interface FlowEdgeOptions {
  border_radius?: number;
  offset?: number;
  step_position?: number;
}

export interface FlowConnection {
  from: string;
  to: string;
  label: string;
  style: "solid" | "dashed";
  tone?: FlowTone;
  // For port-shape decision outputs: ID of the source handle on the
  // decision node (e.g. "port-0", "port-1"). When set, ReactFlow
  // attaches the edge to that specific handle instead of letting the
  // FloatingEdge compute endpoints.
  source_handle?: string;
  marker?: FlowMarker;
  animated?: boolean;
  border_radius?: number;
}

export interface FlowGroup {
  id: string;
  label: string;
  color: string;
}

export interface FlowPalette {
  node_bg: string;
  node_fg: string;
  node_fg_muted: string;
  node_fg_faint: string;
  node_border: string;
}

export interface FlowConfig {
  direction?: FlowDirection;
  layout_engine?: FlowLayoutEngine;
  edge_style?: FlowEdgeStyle;
  edge_options?: FlowEdgeOptions;
  background?: FlowBackground;
  show_minimap?: boolean;
  color_mode?: FlowColorMode;
  // Node-body palette — five resolved colour values. When unset, the
  // diagram uses the bare token defaults from theme.css (light cards).
  palette?: FlowPalette;
  palette_name?: "light" | "dark";
  title_block?: {
    drawing_id?: string;
    rev?: string;
    author?: string;
    date?: string;
  };
}

export interface FlowDefinition {
  process: {
    name: string;
    description: string;
    config: FlowConfig;
    groups: FlowGroup[];
    steps: FlowStep[];
    connections: FlowConnection[];
  };
}
