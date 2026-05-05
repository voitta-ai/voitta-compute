// Chart.js wrapper that renders a normalised PlotSpec into a canvas.
//
// One component per spec. The Chart instance is created in useEffect and
// destroyed on unmount or spec change so we don't leak WebGL contexts.

import { useEffect, useRef } from "preact/hooks";
import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  type ChartConfiguration,
  DoughnutController,
  Filler,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  LogarithmicScale,
  PieController,
  PointElement,
  PolarAreaController,
  RadarController,
  RadialLinearScale,
  ScatterController,
  Title,
  Tooltip,
} from "chart.js";
import { MatrixController, MatrixElement } from "chartjs-chart-matrix";
import annotationPlugin from "chartjs-plugin-annotation";

import type {
  BarsSpec,
  ChartJsSpec,
  HeatmapSpec,
  PieSpec,
  PlotSpec,
  RadarSpec,
  XySpec,
  XyTrace,
} from "../lib/plot-spec";

let registered = false;
function ensureRegistered() {
  if (registered) return;
  Chart.register(
    BarController,
    BarElement,
    LineController,
    LineElement,
    PointElement,
    ScatterController,
    CategoryScale,
    LinearScale,
    LogarithmicScale,
    RadialLinearScale,
    Filler,
    Title,
    Tooltip,
    Legend,
    ArcElement,
    DoughnutController,
    PieController,
    PolarAreaController,
    RadarController,
    MatrixController,
    MatrixElement,
    annotationPlugin,
  );
  registered = true;
}

// 8-color categorical palette (mirrors the original plugin's plot palette).
const PALETTE = [
  "#cf2a2e",
  "#1766c4",
  "#1c8a4a",
  "#b45309",
  "#7a5cd6",
  "#0891b2",
  "#be185d",
  "#475569",
];

function pickColor(i: number, override?: string): string {
  return override || PALETTE[i % PALETTE.length];
}

function dashFor(d?: string): number[] | undefined {
  if (d === "dashed") return [6, 4];
  if (d === "dotted") return [2, 4];
  return undefined;
}

// ---- xy spec → Chart.js config -------------------------------------------

function buildXyConfig(spec: XySpec): ChartConfiguration {
  const baseType = spec.defaultType === "scatter" ? "scatter" : "line";
  const datasets = spec.traces.map((t: XyTrace, i: number) => {
    const color = pickColor(i, t.color);
    const traceType = t.type || baseType;
    const stepped = traceType === "stepped" ? true : undefined;
    const fill =
      t.fill === "origin"
        ? "origin"
        : t.fill === "+1"
          ? "+1"
          : t.fill === "-1"
            ? "-1"
            : traceType === "area"
              ? "origin"
              : false;
    return {
      type: traceType === "scatter" ? "scatter" : "line",
      label: t.label,
      data: t.x.map((x, k) => ({ x, y: t.y[k] })),
      borderColor: color,
      backgroundColor:
        traceType === "area" ? color + "33" : traceType === "scatter" ? color : color,
      borderDash: dashFor(t.line?.dash),
      borderWidth: t.line?.width ?? 2,
      pointRadius: traceType === "scatter" ? (t.marker?.size ?? 3) : 0,
      pointHoverRadius: traceType === "scatter" ? (t.marker?.size ?? 3) + 2 : 4,
      pointBackgroundColor: t.marker?.color || color,
      stepped,
      fill,
      tension: t.line?.smoothing ?? 0,
      yAxisID: t.yAxis === "right" ? "yRight" : "yLeft",
    };
  });

  const annotations: Record<string, any> = {};
  spec.annotations?.forEach((a, i) => {
    annotations[`a${i}`] = {
      type: "label",
      xValue: a.x,
      yValue: a.y,
      content: [a.text],
      color: a.color || "#1C242C",
      backgroundColor: "rgba(255,255,255,0.7)",
      padding: 4,
      font: { size: 11 },
    };
  });
  spec.referenceLines?.forEach((r, i) => {
    annotations[`r${i}`] = {
      type: "line",
      [r.axis === "x" ? "xMin" : "yMin"]: r.at,
      [r.axis === "x" ? "xMax" : "yMax"]: r.at,
      borderColor: r.color || "#cf2a2e",
      borderWidth: 1,
      borderDash: dashFor(r.dash) || [4, 4],
      label: r.label
        ? {
            content: r.label,
            display: true,
            position: "start",
            font: { size: 10 },
          }
        : undefined,
    };
  });

  const usesRight = spec.traces.some((t) => t.yAxis === "right");

  return {
    type: baseType,
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      plugins: {
        title: spec.title
          ? {
              display: true,
              text: spec.title,
              font: { size: 12, weight: "bold" },
            }
          : undefined,
        legend:
          spec.legend?.position === "hidden"
            ? { display: false }
            : {
                display: true,
                position: spec.legend?.position ?? "top",
                labels: { boxWidth: 12, font: { size: 11 } },
              },
        tooltip: { mode: "nearest", intersect: false },
        annotation: { annotations },
      },
      scales: {
        x: {
          type: spec.xAxis?.log ? "logarithmic" : "linear",
          title: spec.xAxis?.label
            ? { display: true, text: spec.xAxis.label }
            : undefined,
          min: spec.xAxis?.min,
          max: spec.xAxis?.max,
          ticks: { maxRotation: 0, autoSkip: true },
        },
        yLeft: {
          position: "left",
          type: spec.yAxisLeft?.log ? "logarithmic" : "linear",
          title: spec.yAxisLeft?.label
            ? { display: true, text: spec.yAxisLeft.label }
            : undefined,
          min: spec.yAxisLeft?.min,
          max: spec.yAxisLeft?.max,
        },
        ...(usesRight
          ? {
              yRight: {
                position: "right",
                type: spec.yAxisRight?.log ? "logarithmic" : "linear",
                title: spec.yAxisRight?.label
                  ? { display: true, text: spec.yAxisRight.label }
                  : undefined,
                min: spec.yAxisRight?.min,
                max: spec.yAxisRight?.max,
                grid: { drawOnChartArea: false },
              },
            }
          : {}),
      },
    },
  } as ChartConfiguration;
}

// ---- bars spec -----------------------------------------------------------

function buildBarsConfig(spec: BarsSpec): ChartConfiguration {
  const horizontal = spec.orientation === "horizontal";
  const datasets = spec.series.map((s, i) => {
    const color = pickColor(i, s.color);
    let backgroundColor: string | string[] = color;
    if (spec.colorByCategory && spec.series.length === 1) {
      backgroundColor = spec.categories.map((_, k) => pickColor(k));
    }
    return {
      label: s.label,
      data: s.values,
      backgroundColor,
      borderColor: backgroundColor,
      stack: spec.stacked ? s.stack || "default" : undefined,
      yAxisID: s.yAxis === "right" ? "yRight" : "yLeft",
    };
  });

  const annotations: Record<string, any> = {};
  spec.referenceLines?.forEach((r, i) => {
    annotations[`r${i}`] = {
      type: "line",
      [horizontal ? "xMin" : "yMin"]: r.at,
      [horizontal ? "xMax" : "yMax"]: r.at,
      borderColor: r.color || "#cf2a2e",
      borderWidth: 1,
      borderDash: [4, 4],
      label: r.label
        ? {
            content: r.label,
            display: true,
            position: "start",
            font: { size: 10 },
          }
        : undefined,
    };
  });

  return {
    type: "bar",
    data: { labels: spec.categories, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      indexAxis: horizontal ? "y" : "x",
      plugins: {
        title: spec.title
          ? {
              display: true,
              text: spec.title,
              font: { size: 12, weight: "bold" },
            }
          : undefined,
        legend:
          spec.legend?.position === "hidden"
            ? { display: false }
            : {
                display: true,
                position: spec.legend?.position ?? "top",
                labels: { boxWidth: 12, font: { size: 11 } },
              },
        annotation: { annotations },
      },
      scales: {
        x: horizontal
          ? {
              type: spec.valueLog ? "logarithmic" : "linear",
              stacked: spec.stacked,
              title: spec.valueLabel
                ? { display: true, text: spec.valueLabel }
                : undefined,
            }
          : {
              type: "category",
              stacked: spec.stacked,
              title: spec.categoryLabel
                ? { display: true, text: spec.categoryLabel }
                : undefined,
              ticks: { autoSkip: false, maxRotation: 60 },
            },
        yLeft: horizontal
          ? {
              position: "left",
              type: "category",
              stacked: spec.stacked,
              title: spec.categoryLabel
                ? { display: true, text: spec.categoryLabel }
                : undefined,
              ticks: { autoSkip: false },
            }
          : {
              position: "left",
              type: spec.valueLog ? "logarithmic" : "linear",
              stacked: spec.stacked,
              title: spec.valueLabel
                ? { display: true, text: spec.valueLabel }
                : undefined,
            },
      },
    },
  } as ChartConfiguration;
}

// ---- heatmap (chartjs-chart-matrix) --------------------------------------

function viridisColor(t: number): string {
  const stops: [number, number, number][] = [
    [68, 1, 84],
    [59, 82, 139],
    [33, 144, 141],
    [93, 201, 99],
    [253, 231, 37],
  ];
  const x = Math.min(1, Math.max(0, t)) * 4;
  const i = Math.min(3, Math.floor(x));
  const f = x - i;
  const a = stops[i];
  const b = stops[i + 1];
  return `rgb(${Math.round(a[0] + (b[0] - a[0]) * f)},${Math.round(
    a[1] + (b[1] - a[1]) * f,
  )},${Math.round(a[2] + (b[2] - a[2]) * f)})`;
}

function divergingColor(t: number): string {
  const x = Math.min(1, Math.max(0, t));
  if (x < 0.5) {
    const k = x * 2;
    return `rgb(${207},${Math.round(42 + (240 - 42) * k)},${Math.round(46 + (240 - 46) * k)})`;
  }
  const k = (x - 0.5) * 2;
  return `rgb(${Math.round(240 - (240 - 28) * k)},${Math.round(240 - (240 - 138) * k)},${Math.round(240 - (240 - 74) * k)})`;
}

function colorFor(scale: HeatmapSpec["colorscale"], t: number): string {
  if (scale === "diverging" || scale === "red-green") return divergingColor(t);
  return viridisColor(t);
}

function buildHeatmapConfig(spec: HeatmapSpec): ChartConfiguration {
  const flat: { x: string; y: string; v: number }[] = [];
  let zMin = spec.zMin ?? Infinity;
  let zMax = spec.zMax ?? -Infinity;
  for (let yi = 0; yi < spec.y.length; yi++) {
    for (let xi = 0; xi < spec.x.length; xi++) {
      const v = spec.z[yi][xi];
      if (spec.zMin === undefined && v < zMin) zMin = v;
      if (spec.zMax === undefined && v > zMax) zMax = v;
      flat.push({ x: spec.x[xi], y: spec.y[yi], v });
    }
  }
  const range = zMax - zMin || 1;
  const data = flat.map((cell) => ({ x: cell.x, y: cell.y, v: cell.v }));

  return {
    type: "matrix",
    data: {
      datasets: [
        {
          label: spec.valueLabel || "value",
          data,
          backgroundColor: (ctx: any) => {
            const v = ctx.raw?.v ?? 0;
            const t = (v - zMin) / range;
            return colorFor(spec.colorscale, t);
          },
          borderColor: "#ffffff",
          borderWidth: 1,
          width: ({ chart }: any) => {
            const x = chart.scales.x;
            return Math.max(2, (x?.width || 200) / spec.x.length - 2);
          },
          height: ({ chart }: any) => {
            const y = chart.scales.y;
            return Math.max(2, (y?.height || 200) / spec.y.length - 2);
          },
        } as any,
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        title: spec.title
          ? {
              display: true,
              text: spec.title,
              font: { size: 12, weight: "bold" },
            }
          : undefined,
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items: any[]) => items[0]?.raw?.x || "",
            label: (item: any) => `${item.raw?.y}: ${item.raw?.v}`,
          },
        },
      },
      scales: {
        x: {
          type: "category",
          labels: spec.x,
          title: spec.xLabel ? { display: true, text: spec.xLabel } : undefined,
          ticks: { autoSkip: false, maxRotation: 60 },
          offset: true,
          grid: { display: false },
        },
        y: {
          type: "category",
          labels: spec.y.slice().reverse(),
          title: spec.yLabel ? { display: true, text: spec.yLabel } : undefined,
          ticks: { autoSkip: false },
          offset: true,
          grid: { display: false },
        },
      },
    },
  } as ChartConfiguration;
}

// ---- pie / donut --------------------------------------------------------

function buildPieConfig(spec: PieSpec): ChartConfiguration {
  const labels = spec.slices.map((s) => s.label);
  const values = spec.slices.map((s) => s.value);
  const colors = spec.slices.map((s, i) => pickColor(i, s.color));
  return {
    type: spec.donut ? "doughnut" : "pie",
    data: {
      labels,
      datasets: [
        {
          data: values,
          backgroundColor: colors,
          borderColor: "#fff",
          borderWidth: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        title: spec.title
          ? {
              display: true,
              text: spec.title,
              font: { size: 12, weight: "bold" },
            }
          : undefined,
        legend:
          spec.legend?.position === "hidden"
            ? { display: false }
            : {
                display: true,
                position: spec.legend?.position ?? "right",
                labels: { boxWidth: 12, font: { size: 11 } },
              },
        tooltip: {
          callbacks: {
            label: (ctx: any) => {
              const total = (ctx.dataset.data as number[]).reduce(
                (a, b) => a + b,
                0,
              ) || 1;
              const v = ctx.raw as number;
              const pct = ((v / total) * 100).toFixed(1);
              return ` ${ctx.label}: ${v} (${pct}%)`;
            },
          },
        },
      },
    },
  } as ChartConfiguration;
}

// ---- radar --------------------------------------------------------------

function buildRadarConfig(spec: RadarSpec): ChartConfiguration {
  const datasets = spec.series.map((s, i) => {
    const color = pickColor(i, s.color);
    const fill = s.fill !== false;
    return {
      label: s.label,
      data: s.values,
      borderColor: color,
      backgroundColor: fill ? color + "33" : "transparent",
      pointBackgroundColor: color,
      borderWidth: 2,
      fill,
    };
  });
  return {
    type: "radar",
    data: { labels: spec.axes, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        title: spec.title
          ? {
              display: true,
              text: spec.title,
              font: { size: 12, weight: "bold" },
            }
          : undefined,
        legend:
          spec.legend?.position === "hidden"
            ? { display: false }
            : {
                display: true,
                position: spec.legend?.position ?? "top",
                labels: { boxWidth: 12, font: { size: 11 } },
              },
      },
      scales: {
        r: {
          min: spec.min,
          max: spec.max,
          ticks: { font: { size: 10 } },
          pointLabels: { font: { size: 11 } },
        },
      },
    },
  } as ChartConfiguration;
}

// ---- chartjs pass-through -----------------------------------------------

function buildChartJsConfig(spec: ChartJsSpec): ChartConfiguration {
  // JSON round-trip strips any function fields (callbacks, formatters) that
  // could be smuggled in via a custom config. The model can only express
  // declarative Chart.js configs through this path.
  let safe: ChartConfiguration;
  try {
    safe = JSON.parse(JSON.stringify(spec.config)) as ChartConfiguration;
  } catch {
    safe = { type: "line", data: { datasets: [] } } as ChartConfiguration;
  }
  const opts = (safe.options ?? {}) as any;
  opts.responsive = true;
  opts.maintainAspectRatio = false;
  opts.animation = false;
  if (spec.title) {
    opts.plugins = opts.plugins || {};
    opts.plugins.title = {
      display: true,
      text: spec.title,
      font: { size: 12, weight: "bold" },
    };
  }
  safe.options = opts;
  return safe;
}

// ---- component -----------------------------------------------------------

export function PlotCard({ spec }: { spec: PlotSpec }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);

  useEffect(() => {
    ensureRegistered();
    const canvas = canvasRef.current;
    if (!canvas) return;
    let config: ChartConfiguration;
    if (spec.kind === "xy") config = buildXyConfig(spec);
    else if (spec.kind === "bars") config = buildBarsConfig(spec);
    else if (spec.kind === "heatmap") config = buildHeatmapConfig(spec);
    else if (spec.kind === "pie") config = buildPieConfig(spec);
    else if (spec.kind === "radar") config = buildRadarConfig(spec);
    else if (spec.kind === "chartjs") config = buildChartJsConfig(spec);
    else return;
    chartRef.current = new Chart(canvas, config);
    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, [spec]);

  const defaultHeight =
    spec.kind === "heatmap"
      ? 320
      : spec.kind === "pie" || spec.kind === "radar"
        ? 280
        : 240;
  const height = (spec as any).height ?? defaultHeight;

  return (
    <div class={`plot-card kind-${spec.kind}`}>
      <div class="plot-canvas-wrap" style={{ height: `${height}px` }}>
        <canvas ref={canvasRef} />
      </div>
    </div>
  );
}
