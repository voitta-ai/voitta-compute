/** @jsxImportSource react */
// Flow-chart renderer — real React island, mounted by FlowReportPane.
//
// Architecture (post-refactor):
//
//   • Single source + single target hidden handle per node. Edges
//     compute their own endpoints via FloatingEdge → getEdgeParams.
//   • Port-shape decisions use FixedEdge instead — each output gets
//     a labeled handle, edges attach explicitly via sourceHandle.
//   • Edge labels render in the DOM via EdgeLabelRenderer, not as
//     SVG <text>. Lets tone classes / CSS variables fully apply.
//   • `nodeOrigin={[0.5, 0.5]}` so layout coords are centre-aligned
//     (we don't subtract half-width in flow-layout.ts anymore).
//   • `colorMode` derived from config (auto → resolve from
//     ctx.get_theme().is_dark at backend time, then passed through).
//   • Title block is a `<Panel position="top-right">` (built-in).
//   • Markers use `MarkerType.ArrowClosed` with `color: null` so the
//     arrowhead inherits `--xy-edge-stroke` automatically — no
//     duplicated colour map.

import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  type ColorMode,
  type Edge,
  type Node,
  type ReactFlowInstance,
} from "@xyflow/react";

import { useCallback, useEffect, useRef, useState } from "react";

import { layoutFlow } from "../lib/flow-layout";
import type {
  FlowConnection,
  FlowDefinition,
  FlowEdgeStyle,
  FlowStep,
} from "../lib/flow-types";
import { flowEdgeTypes } from "./flow-edges";
import { flowNodeTypes } from "./flow-nodes";

interface Props {
  definition: FlowDefinition;
  onReady?: () => void;
  onError?: (err: Error) => void;
}

const ARROW_TYPE: Record<string, MarkerType | null> = {
  "arrow": MarkerType.Arrow,
  "arrow-closed": MarkerType.ArrowClosed,
  "none": null,
};

function connectionToEdge(
  c: FlowConnection,
  defaultEdgeType: FlowEdgeStyle,
  edgeOptions: NonNullable<FlowDefinition["process"]["config"]>["edge_options"] = {},
): Edge {
  const tone = c.tone ?? "default";
  const dashed = c.style === "dashed";
  const isPortBranch = !!c.source_handle;
  const markerType = ARROW_TYPE[c.marker ?? "arrow-closed"] ?? null;

  return {
    id: `${c.from}->${c.to}`,
    source: c.from,
    target: c.to,
    sourceHandle: c.source_handle,
    // Port-decision branches use FixedEdge (explicit handle attach);
    // everything else uses FloatingEdge (computed endpoints).
    type: isPortBranch ? "fixed" : "floating",
    label: c.label || undefined,
    animated: c.animated === true,
    data: {
      edgeType: defaultEdgeType,
      tone,
      borderRadius: c.border_radius ?? edgeOptions.border_radius,
      offset: edgeOptions.offset,
      stepPosition: edgeOptions.step_position,
    },
    style: {
      strokeWidth: 2,
      strokeDasharray: dashed ? "8 5" : undefined,
    },
    markerEnd: markerType
      ? {
          type: markerType,
          // Passing `color: null` makes ReactFlow render the marker
          // with the `--xy-edge-stroke` CSS variable. We override
          // that variable per-tone via a CSS rule on `.flow-edge.tone-*`,
          // so the arrowhead automatically matches the stroke.
          color: null as unknown as string,
          width: 18,
          height: 18,
        }
      : undefined,
    className: `flow-edge tone-${tone}${dashed ? " is-dashed" : ""}`,
  };
}

function stepToNode(
  step: FlowStep,
  x: number,
  y: number,
  width: number,
  height: number,
): Node {
  return {
    id: step.id,
    type: step.type,
    position: { x, y },
    data: { step },
    width,
    height,
    style: { width, height },
  };
}

// Probe whether `--voitta-bg` (read from a node INSIDE the shadow root)
// resolves to a dark or light colour. Returns null if the value can't
// be parsed — caller falls back to "light".
function detectColorMode(el: Element | null): "dark" | "light" | null {
  if (!el || typeof window === "undefined") return null;
  const bg = getComputedStyle(el).getPropertyValue("--voitta-bg").trim();
  if (!bg) return null;
  // Tokens are usually 6-char hex; accept 3-char hex too.
  let m = bg.match(/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
  let r: number, g: number, b: number;
  if (m) {
    r = parseInt(m[1], 16); g = parseInt(m[2], 16); b = parseInt(m[3], 16);
  } else {
    m = bg.match(/^#?([0-9a-f])([0-9a-f])([0-9a-f])$/i);
    if (!m) return null;
    r = parseInt(m[1] + m[1], 16);
    g = parseInt(m[2] + m[2], 16);
    b = parseInt(m[3] + m[3], 16);
  }
  const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  return lum < 0.5 ? "dark" : "light";
}

export function FlowDiagram({ definition, onReady, onError }: Props) {
  const cfg = definition.process.config ?? {};
  const defaultEdgeType: FlowEdgeStyle = cfg.edge_style ?? "smoothstep";
  const bgVariant = cfg.background ?? "dots";
  const showMinimap = !!cfg.show_minimap;
  const titleBlock = cfg.title_block;

  // Wrapper ref lives INSIDE the widget's shadow DOM, so
  // getComputedStyle(wrapperRef.current) resolves :host { --voitta-* }
  // variables correctly. document.documentElement does NOT — that's
  // the host page's <html>, which has no --voitta-* tokens.
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [resolvedMode, setResolvedMode] = useState<ColorMode>("light");

  // For explicit "dark" / "light" / "system" — use directly.
  // For "auto" (or unset → defaults to auto) — probe the wrapper post-mount.
  useEffect(() => {
    const m = cfg.color_mode;
    if (m === "dark" || m === "light" || m === "system") {
      setResolvedMode(m);
      return;
    }
    // auto: probe after mount; rAF gives the DOM one tick to settle
    // any pending plugin-theme-link load.
    const probe = () => {
      const mode = detectColorMode(wrapperRef.current);
      if (mode) setResolvedMode(mode);
    };
    const id = requestAnimationFrame(probe);
    return () => cancelAnimationFrame(id);
  }, [cfg.color_mode]);

  const [layoutState, setLayoutState] = useState<
    | { kind: "loading" }
    | { kind: "ready"; nodes: Node[]; edges: Edge[] }
    | { kind: "error"; message: string }
  >({ kind: "loading" });

  const instanceRef = useRef<ReactFlowInstance | null>(null);
  const onInit = useCallback((instance: ReactFlowInstance) => {
    instanceRef.current = instance;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        instance.fitView({ padding: 0.15, duration: 0 });
      });
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLayoutState({ kind: "loading" });
    (async () => {
      try {
        const lay = await layoutFlow(definition);
        if (cancelled) return;
        const byId = new Map(lay.nodes.map((n) => [n.id, n]));
        const nodes: Node[] = definition.process.steps.map((step) => {
          const pos = byId.get(step.id);
          return stepToNode(
            step,
            pos?.x ?? 0,
            pos?.y ?? 0,
            pos?.width ?? 220,
            pos?.height ?? 80,
          );
        });
        const edges: Edge[] = definition.process.connections.map((c) =>
          connectionToEdge(c, defaultEdgeType, cfg.edge_options),
        );
        setLayoutState({ kind: "ready", nodes, edges });
        onReady?.();
      } catch (err) {
        if (cancelled) return;
        const e = err instanceof Error ? err : new Error(String(err));
        setLayoutState({ kind: "error", message: e.message });
        onError?.(e);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [definition]);

  if (layoutState.kind === "loading") {
    return (
      <div className="flow-loading">
        <div className="flow-loading__spinner" />
        <div className="flow-loading__label">Routing diagram…</div>
      </div>
    );
  }
  if (layoutState.kind === "error") {
    return <pre className="flow-error">Flow layout error:{"\n"}{layoutState.message}</pre>;
  }

  return (
    <ReactFlowProvider>
      <div
        ref={wrapperRef}
        style={{ width: "100%", height: "100%", position: "relative" }}
      >
        <ReactFlow
          nodes={layoutState.nodes}
          edges={layoutState.edges}
          nodeTypes={flowNodeTypes}
          edgeTypes={flowEdgeTypes}
          // Centre-anchored positions: the layout pass returns the
          // CENTRE of each node (we no longer subtract half-w/half-h).
          nodeOrigin={[0.5, 0.5]}
          // Arrowheads with explicit `color: null` will render using
          // this CSS variable. We override `--xy-edge-stroke` per-tone
          // in styles.css so arrowheads automatically follow the
          // stroke colour.
          defaultMarkerColor={null as unknown as string}
          colorMode={resolvedMode}
          elevateEdgesOnSelect={true}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          minZoom={0.2}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={true}
          panOnDrag={true}
          panOnScroll={false}
          zoomOnScroll={true}
          zoomOnPinch={true}
          zoomOnDoubleClick={true}
          deleteKeyCode={null}
          selectionKeyCode={null}
          multiSelectionKeyCode={null}
          panActivationKeyCode={null}
          zoomActivationKeyCode={null}
          onInit={onInit}
        >
          {bgVariant !== "none" && (
            <Background
              variant={bgVariant as BackgroundVariant}
              gap={16}
              size={1}
              color="var(--voitta-flow-grid, #d4d4d8)"
            />
          )}
          <Controls showInteractive={false} position="bottom-left" />
          {showMinimap && (
            <MiniMap
              pannable
              zoomable
              position="bottom-right"
              maskColor="rgba(15, 23, 42, 0.6)"
              nodeColor={() => "#1e293b"}
              nodeStrokeColor={() => "#94a3b8"}
            />
          )}
          {titleBlock && (
            <Panel position="top-right" className="flow-titleblock">
              {titleBlock.drawing_id && (
                <div className="flow-titleblock__row">
                  <span className="flow-titleblock__key">DWG</span>
                  <span className="flow-titleblock__val">{titleBlock.drawing_id}</span>
                </div>
              )}
              {titleBlock.rev && (
                <div className="flow-titleblock__row">
                  <span className="flow-titleblock__key">REV</span>
                  <span className="flow-titleblock__val">{titleBlock.rev}</span>
                </div>
              )}
              {titleBlock.author && (
                <div className="flow-titleblock__row">
                  <span className="flow-titleblock__key">BY</span>
                  <span className="flow-titleblock__val">{titleBlock.author}</span>
                </div>
              )}
              {titleBlock.date && (
                <div className="flow-titleblock__row">
                  <span className="flow-titleblock__key">DATE</span>
                  <span className="flow-titleblock__val">{titleBlock.date}</span>
                </div>
              )}
            </Panel>
          )}
        </ReactFlow>
      </div>
    </ReactFlowProvider>
  );
}
