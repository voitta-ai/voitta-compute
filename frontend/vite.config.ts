import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

// Build a single, self-contained IIFE that the bookmarklet `<script>`-loads
// from the FastAPI backend. No code splitting, no module imports at runtime —
// the bundle must work when injected into any third-party page.
//
// Plugin files live OUTSIDE the frontend/ tree (one repo up at
// ../plugins/<name>/frontend/*.tsx) and Rollup resolves their bare
// imports with standard Node walk-up. Without an alias the walk-up
// can't find ``preact/jsx-runtime`` — there's no node_modules above
// the plugin dir. We map every subpath we actually use to the
// frontend's installed copy so plugin .tsx files compile against the
// same preact runtime as the core bundle.
const F = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      "preact/jsx-runtime": `${F}node_modules/preact/jsx-runtime/dist/jsxRuntime.mjs`,
      "preact/hooks": `${F}node_modules/preact/hooks/dist/hooks.mjs`,
      "preact/compat": `${F}node_modules/preact/compat/dist/compat.mjs`,
      preact: `${F}node_modules/preact/dist/preact.mjs`,
      // No `react`/`react-dom` aliases. ReactFlow needs real React to
      // function (preact/compat breaks ReactFlow's d3-zoom pan/zoom
      // handlers — wheel events reach the DOM but never trigger
      // viewport changes). We pay the ~140 KB gzipped cost to get a
      // working flow-chart canvas. Real React is mounted as an
      // ISLAND inside the Preact tree via ReactDOM.createRoot in
      // FlowReportPane — only the flow components themselves are
      // React. Everything else stays Preact.
    },
    dedupe: ["preact"],
  },
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "preact",
  },
  // ReactFlow + ELK + dagre reference `process.env.NODE_ENV` at module
  // top level (React-ecosystem convention). Vite's lib mode doesn't
  // automatically strip these for IIFE builds — the host page has no
  // Node `process` global, so the bundle ReferenceError's at boot.
  // Replace literally at build time.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
    "process.platform": JSON.stringify("browser"),
  },
  build: {
    target: "es2020",
    outDir: "dist",
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: true,
    lib: {
      entry: "src/main.tsx",
      name: "VoittaBookmarklet",
      formats: ["iife"],
      fileName: () => "widget.js",
    },
    rollupOptions: {
      output: {
        // Inline the small CSS file — the widget owns its Shadow DOM, so we
        // need the styles available at boot, not loaded as a sibling asset.
        assetFileNames: "[name].[ext]",
        inlineDynamicImports: true,
      },
    },
  },
  server: {
    port: 5173,
  },
});
