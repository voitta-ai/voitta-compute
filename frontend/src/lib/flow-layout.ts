// Layout pass — assigns {x, y} to each node before ReactFlow renders.
//
// Two engines, configurable per flow:
//
//   • elk (default)  — elkjs, Sugiyama-style layered routing. Better
//                       at orthogonal edge crossings; better at long-
//                       label-aware spacing. ~250 KB.
//   • dagre          — smaller, faster, slightly cruder edge routing.
//                       Good fallback for huge graphs.
//
// Both produce the same output shape — {nodes: [{id, x, y}], …} — so
// the caller (FlowDiagram) doesn't care which ran.
//
// Node dimensions: we estimate per-node width/height from the rendered
// content shape (label length + meta rows + badges). The estimate is
// only used by the layout pass — the real node decides its own size at
// render time. Layout overshoot is fine; undershoot causes crowding.

import type {
  FlowDefinition,
  FlowDirection,
  FlowLayoutEngine,
  FlowStep,
} from "./flow-types";

export interface LaidOutNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LaidOutEdge {
  // Stable id matching connection (`${from}->${to}`).
  id: string;
  // ELK orthogonal-routing bend points (absolute, post-layout). Empty
  // when the layout engine doesn't produce them (dagre, or ELK with
  // non-orthogonal routing).
  bendPoints: { x: number; y: number }[];
  // Source and target attach points (absolute). When present, the
  // OrthogonalEdge component uses them as line endpoints instead of
  // letting floating-edge math approximate.
  sourcePoint?: { x: number; y: number };
  targetPoint?: { x: number; y: number };
}

export interface LayoutResult {
  nodes: LaidOutNode[];
  edges: LaidOutEdge[];
}

// Size estimate based on content. Has to match what the rendered DOM
// actually takes — if we estimate too small, edges route into empty
// space (the node visually overflows the layout slot) and fitView
// fits an undersized bounding box. Overshoot is fine; undershoot
// breaks the diagram visually.
function estimateNodeSize(step: FlowStep): { width: number; height: number } {
  // Decision-shape variants have very different layout footprints.
  if (step.type === "decision" && step.shape) {
    if (step.shape === "junction") {
      // Just a small circle with the label below. Width = max(48,
      // label width); height = 44 (dot) + 18 (label gap).
      const w = Math.max(48, step.label.length * 7 + 16);
      return { width: w, height: 62 };
    }
    if (step.shape === "diamond") {
      // Diamond bounding box must be big enough that the inscribed
      // axis-aligned rectangle (W/2 × H/2 for a W×H rhombus) holds
      // three lines of text (type chip, label, id). Content estimate:
      //   • label is the widest line (~7.5 px/char + small padding)
      //   • height is ~52 px for three lines at 9/13/9.5 px font sizes
      //
      // We size width and height independently — a long label produces
      // a wide diamond, not a square one. This makes diamonds with
      // 1–2 word labels compact (under 220 px wide) and only the long
      // ones stretch out.
      const labelW = step.label.length * 7.5 + 12;
      const contentW = Math.max(110, labelW);   // inscribed-rect width
      const contentH = 56;                      // inscribed-rect height
      const W = Math.min(360, Math.max(180, contentW * 2 + 24));
      const H = Math.max(140, contentH * 2 + 16);
      return { width: W, height: H };
    }
    if (step.shape === "port") {
      // Per-branch row in body. Width is the widest of (title, label,
      // longest branch label + handle gutter). Height = header + label
      // + (rows * 26).
      const branchCount = (step.conditions ?? []).length;
      const longestBranch = (step.conditions ?? []).reduce(
        (m, c) => Math.max(m, c.label.length),
        0,
      );
      const widestText = Math.max(
        step.label.length,
        longestBranch + 8,
        step.id.length + 14,
      );
      const width = Math.min(360, Math.max(220, widestText * 8 + 40));
      const height = 26 + 28 + branchCount * 28 + 18;
      return { width, height };
    }
    // shape === "rect" — falls through to the default branch below.
  }

  const HEADER_H = 26;
  const LABEL_H = 22;
  const META_ROW_H = 18;
  const BADGE_ROW_H = 26;
  const NOTE_H = 28;
  const INFERRED_ROW_H = 18;  // roles / in / out
  const BODY_PAD = 22;        // vertical padding of .flow-node__body (9 + 10 + a bit)

  // Width — 7.5 px/char for body text (Inter), 7 px/char for monospace,
  // pick the widest line of content.
  const labelChars = step.label.length;
  const longestMeta = (step.meta ?? []).reduce(
    (m, r) => Math.max(m, r.key.length + r.value.length + 4),
    0,
  );
  const longestBadgeRow = (step.badges ?? []).reduce(
    (sum, b) => sum + b.label.length + 4,  // pill incl. padding & gap
    0,
  );
  // Inferred meta (roles/in/out) — value joined by ", "
  const rolesLen = (step.roles ?? []).join(", ").length + 8;
  const inLen = (step.artifacts_in ?? []).join(", ").length + 6;
  const outLen = (step.artifacts_out ?? []).join(", ").length + 6;
  const noteLen = (step.note ?? "").length;
  const idChars = step.id.length + 14;  // step ID + TYPE chip

  const charsWide = Math.max(
    labelChars,
    longestMeta,
    longestBadgeRow,
    rolesLen, inLen, outLen,
    noteLen,
    idChars,
  );

  const width = Math.min(360, Math.max(220, charsWide * 7.5 + 28));

  let height = HEADER_H + LABEL_H + BODY_PAD;
  if ((step.badges ?? []).length > 0) height += BADGE_ROW_H;
  if ((step.roles ?? []).length > 0) height += INFERRED_ROW_H;
  if ((step.artifacts_in ?? []).length > 0) height += INFERRED_ROW_H;
  if ((step.artifacts_out ?? []).length > 0) height += INFERRED_ROW_H;
  if ((step.meta ?? []).length > 0) height += step.meta!.length * META_ROW_H + 4;
  if (step.note) height += NOTE_H;

  return { width, height };
}

// ─── dagre ────────────────────────────────────────────────────────────────


async function layoutWithDagre(
  def: FlowDefinition,
  direction: FlowDirection,
): Promise<LayoutResult> {
  // Dynamic import so dagre is only fetched when actually needed. The
  // bundler still inlines it (the whole flow path is one IIFE), but
  // imports stay tree-shake-friendly.
  const dagre = await import("dagre");
  const g = new dagre.graphlib.Graph({ multigraph: true, compound: true });
  g.setGraph({
    rankdir: direction,
    nodesep: 60,
    ranksep: 90,
    marginx: 24,
    marginy: 24,
  });
  g.setDefaultEdgeLabel(() => ({}));

  for (const step of def.process.steps) {
    const sz = estimateNodeSize(step);
    g.setNode(step.id, { width: sz.width, height: sz.height });
  }
  for (const e of def.process.connections) {
    g.setEdge(e.from, e.to);
  }

  dagre.layout(g);
  const nodes: LaidOutNode[] = def.process.steps.map((step) => {
    const n = g.node(step.id);
    const sz = estimateNodeSize(step);
    // ReactFlow is configured with nodeOrigin=[0.5, 0.5] in
    // FlowDiagram, so positions are interpreted as the node CENTRE.
    // Dagre returns centre points natively, so no subtraction needed.
    return {
      id: step.id,
      x: n?.x ?? 0,
      y: n?.y ?? 0,
      width: sz.width,
      height: sz.height,
    };
  });
  // Dagre exposes per-edge `points` arrays but they're spline control
  // points, not orthogonal bends. Skip them — the frontend uses
  // FloatingEdge / FixedEdge smoothstep paths for dagre layouts.
  return { nodes, edges: [] };
}


// ─── elk ──────────────────────────────────────────────────────────────────


async function layoutWithElk(def: FlowDefinition): Promise<LayoutResult> {
  const ELKMod = await import("elkjs/lib/elk.bundled.js");
  const ELK = (ELKMod.default ?? (ELKMod as any).ELK ?? ELKMod) as any;
  const elk = new ELK();

  const cfg = def.process.config ?? {};
  const direction = cfg.direction ?? "TB";

  // ELK uses a different direction vocabulary. Map ours onto theirs.
  const elkDir = (
    {
      TB: "DOWN",
      LR: "RIGHT",
      BT: "UP",
      RL: "LEFT",
    } as const
  )[direction];

  // Map our edge_routing names to ELK's enum values.
  const elkRouting = (
    {
      orthogonal: "ORTHOGONAL",
      polyline: "POLYLINE",
      splines: "SPLINES",
    } as const
  )[cfg.edge_routing ?? "orthogonal"];

  const sizes = new Map<string, { width: number; height: number }>();
  for (const step of def.process.steps) {
    sizes.set(step.id, estimateNodeSize(step));
  }

  // Compose layout options from script-supplied knobs + sensible
  // defaults. Defaults match the "engineering schematic" aesthetic:
  // generous spacing so orthogonal edges have room to bend.
  const nodeSpacing = String(cfg.node_spacing ?? 60);
  const layerSpacing = String(cfg.layer_spacing ?? 90);
  const crossingMinStrategy = cfg.crossing_minimization ?? "LAYER_SWEEP";
  const nodePlacement = cfg.node_placement ?? "NETWORK_SIMPLEX";
  const thoroughness = String(cfg.thoroughness ?? 7);

  const layoutOptions: Record<string, string> = {
    "elk.algorithm": "layered",
    "elk.direction": elkDir,
    "elk.edgeRouting": elkRouting,
    "elk.spacing.nodeNode": nodeSpacing,
    "elk.layered.spacing.nodeNodeBetweenLayers": layerSpacing,
    "elk.spacing.edgeNode": "30",
    "elk.spacing.edgeEdge": "20",
    "elk.layered.spacing.edgeEdgeBetweenLayers": "20",
    "elk.layered.spacing.edgeNodeBetweenLayers": "30",
    "elk.layered.nodePlacement.strategy": nodePlacement,
    "elk.layered.crossingMinimization.strategy": crossingMinStrategy,
    "elk.layered.thoroughness": thoroughness,
    // FIXED_SIDE lets ELK pick smart port positions when a node has
    // many outputs (port-shape decisions) — places them on one side
    // instead of scattering across all four.
    "elk.portConstraints": "FIXED_SIDE",
  };
  // Per-step elk_options escape hatch (validated server-side).
  if (cfg.elk_options) {
    for (const [k, v] of Object.entries(cfg.elk_options)) {
      layoutOptions[k] = String(v);
    }
  }

  // Map per-edge source_side/target_side hints to ELK port constraints.
  // We synthesise a single dummy port per side the script wanted to
  // pin, with explicit side constraints.
  const elkSideToken: Record<string, string> = {
    top: "NORTH",
    right: "EAST",
    bottom: "SOUTH",
    left: "WEST",
  };

  const graph: any = {
    id: "root",
    layoutOptions,
    children: def.process.steps.map((step) => {
      const sz = sizes.get(step.id)!;
      return { id: step.id, width: sz.width, height: sz.height };
    }),
    edges: def.process.connections.map((e, i) => {
      const edge: any = {
        id: `${e.from}->${e.to}#${i}`,
        sources: [e.from],
        targets: [e.to],
      };
      // Side hints: ELK reads `properties.{port,side}` on each edge.
      // We pass plain `org.eclipse.elk.layered.priority.{direction}`
      // style hints — for stable behaviour we use the layoutOptions
      // map on the edge directly.
      const edgeLayoutOptions: Record<string, string> = {};
      if (e.source_side && elkSideToken[e.source_side]) {
        edgeLayoutOptions["org.eclipse.elk.port.side"] = elkSideToken[e.source_side];
      }
      if (e.target_side && elkSideToken[e.target_side]) {
        edgeLayoutOptions["org.eclipse.elk.layered.allowNonFlowPortsToSwitchSides"] = "true";
      }
      if (Object.keys(edgeLayoutOptions).length > 0) {
        edge.layoutOptions = edgeLayoutOptions;
      }
      return edge;
    }),
  };

  const out = await elk.layout(graph);
  const childById = new Map<string, any>();
  for (const c of out.children ?? []) childById.set(c.id, c);

  const nodes: LaidOutNode[] = def.process.steps.map((step) => {
    const c = childById.get(step.id) || {};
    const sz = sizes.get(step.id)!;
    return {
      id: step.id,
      x: (c.x ?? 0) + sz.width / 2,
      y: (c.y ?? 0) + sz.height / 2,
      width: sz.width,
      height: sz.height,
    };
  });

  // Pull bend points out of ELK's edge results. ELK gives each edge
  // a `sections` array with start/end/bendPoints — we collapse to a
  // flat polyline including the start and end coordinates so the
  // frontend can render with a single <path d="M…L…L…"> string.
  const edges: LaidOutEdge[] = (out.edges ?? []).map((elkEdge: any) => {
    const section = (elkEdge.sections ?? [])[0];
    if (!section) {
      return { id: elkEdge.id, bendPoints: [] };
    }
    const bendPoints = (section.bendPoints ?? []).map((p: any) => ({
      x: Number(p.x) || 0,
      y: Number(p.y) || 0,
    }));
    return {
      id: elkEdge.id,
      bendPoints,
      sourcePoint: section.startPoint
        ? { x: Number(section.startPoint.x), y: Number(section.startPoint.y) }
        : undefined,
      targetPoint: section.endPoint
        ? { x: Number(section.endPoint.x), y: Number(section.endPoint.y) }
        : undefined,
    };
  });

  return { nodes, edges };
}


// ─── dispatch ────────────────────────────────────────────────────────────


export async function layoutFlow(def: FlowDefinition): Promise<LayoutResult> {
  const cfg = def.process.config ?? {};
  const engine: FlowLayoutEngine = cfg.layout_engine ?? "elk";
  const direction: FlowDirection = cfg.direction ?? "TB";
  if (engine === "dagre") return layoutWithDagre(def, direction);
  return layoutWithElk(def);
}
