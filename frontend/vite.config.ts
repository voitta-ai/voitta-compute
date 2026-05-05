import { defineConfig } from "vite";

// Build a single, self-contained IIFE that the bookmarklet `<script>`-loads
// from the FastAPI backend. No code splitting, no module imports at runtime —
// the bundle must work when injected into any third-party page.
export default defineConfig({
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "preact",
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
