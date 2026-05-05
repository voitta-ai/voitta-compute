// Plot spec types — shared between the plot primitives and the PlotCard
// component. The model never constructs a PlotSpec directly — it goes
// through the LLM-facing tools whose schemas are richer; the primitives
// validate + normalise before storing.
//
// (3D plots intentionally omitted from this scaffold — see
// docs/04-tool-catalog.md. They depend on Three.js, which doubles the
// bundle size and isn't needed for the current parser/buffer flow.)

export type PlotKind = "xy" | "bars" | "heatmap" | "pie" | "radar" | "chartjs";

export interface XyTrace {
  label?: string;
  x: number[];
  y: number[];
  color?: string;
  type?: "line" | "scatter" | "area" | "stepped";
  marker?: { shape?: string; size?: number; color?: string };
  line?: { width?: number; dash?: "solid" | "dashed" | "dotted"; smoothing?: number };
  fill?: "none" | "origin" | "+1" | "-1";
  errorY?: { values: number[]; color?: string };
  yAxis?: "left" | "right";
}

export interface XyAxisSpec {
  label?: string;
  log?: boolean;
  min?: number;
  max?: number;
  tickFormat?: "auto" | "sci" | "date" | "percent";
}

export interface XyAnnotation {
  x?: number;
  y?: number;
  text: string;
  color?: string;
}

export interface XyReferenceLine {
  at: number;
  axis: "x" | "y";
  label?: string;
  color?: string;
  dash?: "solid" | "dashed" | "dotted";
}

export interface XySpec {
  kind: "xy";
  title?: string;
  traces: XyTrace[];
  defaultType?: "line" | "scatter" | "area";
  xAxis?: XyAxisSpec;
  yAxisLeft?: XyAxisSpec;
  yAxisRight?: XyAxisSpec;
  annotations?: XyAnnotation[];
  referenceLines?: XyReferenceLine[];
  legend?: { position?: "top" | "right" | "bottom" | "hidden"; maxItems?: number };
  height?: number;
}

export interface BarsSeries {
  label?: string;
  values: number[];
  color?: string;
  stack?: string;
  errorBars?: number[];
  yAxis?: "left" | "right";
}

export interface BarsSpec {
  kind: "bars";
  title?: string;
  categories: string[];
  series: BarsSeries[];
  orientation?: "vertical" | "horizontal";
  stacked?: boolean;
  colorByCategory?: boolean;
  showValueLabels?: boolean;
  categoryLabel?: string;
  valueLabel?: string;
  valueLog?: boolean;
  referenceLines?: { at: number; label?: string; color?: string }[];
  legend?: { position?: "top" | "right" | "bottom" | "hidden" };
  height?: number;
}

export interface HeatmapSpec {
  kind: "heatmap";
  title?: string;
  x: string[]; // column labels
  y: string[]; // row labels
  z: number[][]; // y.length × x.length
  colorscale?: "viridis" | "plasma" | "magma" | "cividis" | "diverging" | "red-green";
  zMin?: number;
  zMax?: number;
  showValues?: boolean;
  xLabel?: string;
  yLabel?: string;
  valueLabel?: string;
  height?: number;
}

export interface PieSlice {
  label: string;
  value: number;
  color?: string;
}

export interface PieSpec {
  kind: "pie";
  title?: string;
  slices: PieSlice[];
  donut?: boolean;
  showValueLabels?: boolean;
  legend?: { position?: "top" | "right" | "bottom" | "hidden" };
  height?: number;
}

export interface RadarSeries {
  label?: string;
  values: number[]; // length must equal axes.length
  color?: string;
  fill?: boolean; // default true
}

export interface RadarSpec {
  kind: "radar";
  title?: string;
  axes: string[];
  series: RadarSeries[];
  min?: number;
  max?: number;
  legend?: { position?: "top" | "right" | "bottom" | "hidden" };
  height?: number;
}

export interface ChartJsSpec {
  kind: "chartjs";
  title?: string;
  config: {
    type: string;
    data: unknown;
    options?: unknown;
  };
  height?: number;
}

export type PlotSpec = XySpec | BarsSpec | HeatmapSpec | PieSpec | RadarSpec | ChartJsSpec;

export interface RichOutput {
  kind: "plot" | "text" | "log" | "image";
  // plot
  plot?: PlotSpec;
  plot_id?: string;
  // text / log
  markdown?: string;
  log_lines?: string[];
  // image — `url` is typically server-relative (e.g.
  // "/api/script-output/<run_id>/img_1.png"); the renderer resolves it
  // against the bookmarklet's backend origin.
  url?: string;
  alt?: string;
}
