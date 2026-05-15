"""User-defined Python scripts — compute scripts and report scripts.

Two flavours, one host:

  • **Compute scripts** define ``run(ctx, args=None)`` and return a small
    JSON-serialisable value. Side-effects via ``ctx.text(md) /
    ctx.image(fig|bytes) / ctx.log(s)`` flow back to the chat as inline
    rich blocks.

  • **Report scripts** define ``build(ctx)`` and return a Panel layout.
    The Panel-served route ``/panel/reports?id=<slug>`` (mounted via
    ``panel.io.fastapi.add_applications``) calls ``build(ctx)`` once per
    browser session and serves the layout live, so EditableTemplate's
    drag/resize commits round-trip to Python.

Persistence layout — one folder per script, with run output co-located::

    scripts/
    ├── compute/
    │   └── <slug>/
    │       ├── code.py
    │       ├── meta.json
    │       └── runs/<run_id>/img_*.png
    └── reports/
        └── <slug>/
            ├── code.py
            └── meta.json

Image outputs from compute scripts land under
``scripts/compute/<slug>/runs/<run_id>/img_N.png`` and are served by
the ``/api/script-output/<slug>/<run_id>/<file>`` route.

Trust model — same as ``buffer_eval``:

  • In-process execution; full venv import surface; no sandbox.
  • Per-script timeout enforced via ``asyncio.wait_for`` over a
    ThreadPoolExecutor. **Note**: the executor thread keeps running
    after timeout (Python doesn't expose a kill primitive); the
    coroutine returns and the orchestrator continues. A truly
    pathological infinite loop would hold a thread until the dev
    restarts the backend — acceptable for v1.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import time
import traceback
from pathlib import Path
from typing import Any


# Project layout — same conventions as services/python_storage.py:
# anchor at ``app.config.PROJECT_ROOT`` so packaged (.app) and dev
# modes both write to a directory the user owns. Computing parents from
# ``__file__`` lands inside the read-only .app Resources bundle on
# packaged installs, which fails with EROFS the moment we try to
# create scripts/compute/<slug>/.
from app.config import PROJECT_ROOT  # noqa: E402

SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
SCRIPTS_COMPUTE = SCRIPTS_ROOT / "compute"
SCRIPTS_REPORTS = SCRIPTS_ROOT / "reports"
SCRIPTS_FLOWS = SCRIPTS_ROOT / "flows"

# Timeouts. Both are async wait_for'd; the underlying thread isn't killed.
COMPUTE_TIMEOUT_S = 60.0
COMPUTE_TIMEOUT_MAX_S = 300.0
REPORT_TIMEOUT_S = 120.0
REPORT_TIMEOUT_MAX_S = 300.0

# Per-run output caps so a runaway script can't fill memory.
MAX_LOG_LINES = 200
MAX_LOG_LINE_BYTES = 1000
MAX_TEXT_BLOCKS = 100
MAX_IMAGES = 20

_SLUG_OK = re.compile(r"^[a-z0-9_-]+$")


# ---- helpers --------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dirs() -> None:
    SCRIPTS_COMPUTE.mkdir(parents=True, exist_ok=True)
    SCRIPTS_REPORTS.mkdir(parents=True, exist_ok=True)
    SCRIPTS_FLOWS.mkdir(parents=True, exist_ok=True)


_KIND_DIRS = {
    "compute": SCRIPTS_COMPUTE,
    "reports": SCRIPTS_REPORTS,
    "flow": SCRIPTS_FLOWS,
}


def slugify(name: str) -> str:
    """Reduce ``name`` to a filesystem-safe slug.

    Rules: lowercase, ``[a-z0-9_-]`` only, runs of separators collapsed,
    leading/trailing ``_-`` stripped, max 64 chars. Empty results
    rejected with ``ValueError``.
    """

    if not isinstance(name, str):
        raise ValueError("name must be a string")
    s = re.sub(r"[^a-z0-9_-]+", "_", name.lower())
    s = re.sub(r"_+", "_", s).strip("_-")
    if not s:
        raise ValueError("name slugifies to empty")
    return s[:64]


def _script_dir(kind: str, slug: str) -> Path:
    try:
        base = _KIND_DIRS[kind]
    except KeyError as exc:
        raise ValueError(f"invalid script kind {kind!r}") from exc
    return base / slug


def _meta_path(kind: str, slug: str) -> Path:
    return _script_dir(kind, slug) / "meta.json"


def _code_path(kind: str, slug: str) -> Path:
    return _script_dir(kind, slug) / "code.py"


def _runs_dir(kind: str, slug: str) -> Path:
    return _script_dir(kind, slug) / "runs"


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_meta(path: Path, meta: dict) -> None:
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


def _persist(kind: str, slug: str, code: str) -> tuple[Path, dict]:
    """Write the script + sidecar meta. Last-write-wins (overwrites). Returns
    ``(code_path, meta_dict)``."""

    _ensure_dirs()
    _script_dir(kind, slug).mkdir(parents=True, exist_ok=True)
    code_path = _code_path(kind, slug)
    meta_path = _meta_path(kind, slug)

    meta = _read_meta(meta_path)
    new_meta = {
        "name": slug,
        "kind": kind,
        "created_at": meta.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
        "code_bytes": len(code.encode("utf-8")),
        # Run history fields are populated by run_compute / report_handler.
        "last_run_at": meta.get("last_run_at"),
        "last_run_ok": meta.get("last_run_ok"),
        "last_run_id": meta.get("last_run_id"),
        "last_run_elapsed_s": meta.get("last_run_elapsed_s"),
    }
    code_path.write_text(code)
    _write_meta(meta_path, new_meta)
    return code_path, new_meta


def _update_run_meta(kind: str, slug: str, *, ok: bool, run_id: str, elapsed_s: float, error: str | None) -> None:
    path = _meta_path(kind, slug)
    meta = _read_meta(path)
    meta["last_run_at"] = _now_iso()
    meta["last_run_ok"] = ok
    meta["last_run_id"] = run_id
    meta["last_run_elapsed_s"] = elapsed_s
    if error:
        meta["last_run_error"] = error[:500]
    else:
        meta.pop("last_run_error", None)
    _write_meta(path, meta)


# ---- ScriptContext --------------------------------------------------------


class ScriptError(RuntimeError):
    """Raised inside ``_run_*_blocking`` when the script itself raises;
    carries the truncated traceback as its message."""


class ScriptContext:
    """The single object scripts use to interact with the host.

    Methods:

      • ``snapshot(handle)``  — return the python_storage record
      • ``dataframe(handle)`` — return ``pd.read_pickle(curves.pkl)``
      • ``raw(handle)``       — return the parsed ``raw.json``
      • ``text(markdown)``    — emit an inline text/markdown block
      • ``image(fig_or_bytes, alt?)`` — emit an inline image; saves to
                                         disk and returns the URL path
      • ``log(*args)``        — append a debug log line
    """

    def __init__(self, run_id: str, *, kind: str, slug: str) -> None:
        self.run_id = run_id
        self.kind = kind
        self.slug = slug
        self.output_dir = _runs_dir(kind, slug) / run_id
        self._image_count = 0
        self._items: list[dict] = []
        self._log_lines: list[str] = []
        # External <script src=...> entries to inject into the report
        # iframe's <head>. Populated by ctx.add_js(); merged into the
        # Panel template by panel_app._wrap_template after build() runs.
        self._js_files: dict[str, str] = {}
        # Raw <style>-block strings to inject into the report iframe's
        # <head>. Populated by ctx.add_css(); merged into
        # template.config.raw_css by panel_app._wrap_template. This is
        # the ONLY safe path for outer-document CSS — pn.pane.HTML(
        # "<style>…") entity-encodes the text (Panel sanitises HTML
        # panes) so the browser sees literal "<style>…" inside a
        # <div>, not a stylesheet, and the rules never reach Tabulator
        # / Bokeh widgets that live in the outer document.
        #
        # NB: ``add_css`` only reaches non-shadow widgets. For shadow-
        # DOM widgets (Tabulator, Bokeh DataTable) pass CSS via the
        # widget's ``stylesheets=`` kwarg — ``ctx.apply_theme`` does
        # both in one call.
        self._raw_css: list[str] = []

    # ---- data access -----------------------------------------------------

    def snapshot(self, handle: str) -> dict:
        from app.services import python_storage

        rec = python_storage.get(handle)
        if rec is None:
            raise KeyError(f"no python_storage snapshot {handle!r}")
        return rec

    def dataframe(self, handle: str):
        import pandas as pd

        rec = self.snapshot(handle)
        pkl = Path(rec["path"]) / "curves.pkl"
        if not pkl.exists():
            raise FileNotFoundError(
                f"snapshot {handle!r} has no curves.pkl (kind not 'curves'?)"
            )
        return pd.read_pickle(pkl)

    def raw(self, handle: str) -> Any:
        rec = self.snapshot(handle)
        path = Path(rec["path"]) / "raw.json"
        if not path.exists():
            raise FileNotFoundError(f"snapshot {handle!r} has no raw.json")
        return json.loads(path.read_text())

    # ---- output ---------------------------------------------------------

    def text(self, markdown: str) -> None:
        if len([i for i in self._items if i.get("kind") == "text"]) >= MAX_TEXT_BLOCKS:
            return
        self._items.append({"kind": "text", "markdown": str(markdown)})

    def image(self, fig_or_bytes: Any, alt: str | None = None) -> str:
        """Save an image to ``output_dir`` and emit an inline image block.

        Accepts a matplotlib ``Figure``, a PIL ``Image``, or raw PNG/JPEG
        bytes. Returns the public URL path (``/api/script-output/...``).
        """

        if self._image_count >= MAX_IMAGES:
            raise RuntimeError(
                f"ctx.image() called more than {MAX_IMAGES} times in one run"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._image_count += 1
        name = f"img_{self._image_count}.png"
        out_path = self.output_dir / name

        if hasattr(fig_or_bytes, "savefig"):  # matplotlib Figure
            fig_or_bytes.savefig(out_path, format="png", bbox_inches="tight")
        elif hasattr(fig_or_bytes, "save"):  # PIL.Image
            fig_or_bytes.save(out_path, format="PNG")
        elif isinstance(fig_or_bytes, (bytes, bytearray)):
            out_path.write_bytes(bytes(fig_or_bytes))
        else:
            raise TypeError(
                "ctx.image() expected matplotlib.Figure / PIL.Image / bytes; "
                f"got {type(fig_or_bytes).__name__}"
            )
        url = f"/api/script-output/{self.slug}/{self.run_id}/{name}"
        self._items.append({"kind": "image", "url": url, "alt": alt})
        return url

    def add_js(self, name: str, url: str) -> None:
        """Request an external ``<script src="...">`` in the report iframe.

        Only meaningful inside a report ``build(ctx)`` — compute scripts
        write into the chat stream, not into a Panel template.

        Use this when your report needs a JS library that isn't already
        bundled by Panel (Three.js, D3, custom rendering libs). The
        script is added to the template's ``<head>`` via Panel's
        ``js_files`` mechanism, so its globals (e.g. ``window.THREE``)
        are available by the time your ``pn.pane.HTML`` boot snippet
        mounts.

        ``name`` must be a Python identifier (Panel uses it as the
        internal resource key). ``url`` is any HTTPS-reachable URL.

        Example::

            def build(ctx):
                import panel as pn
                ctx.add_js("three",
                    "https://cdn.jsdelivr.net/npm/three@0.150.1/build/three.min.js")
                return pn.pane.HTML('''
                    <div id="scene" style="width:100%;height:480px"></div>
                    <script>
                      const r = new THREE.WebGLRenderer({antialias:true});
                      r.setSize(window.innerWidth, 480);
                      document.getElementById("scene").appendChild(r.domElement);
                      // ... scene setup ...
                    </script>
                ''')
        """

        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(
                "ctx.add_js name must be a Python identifier (e.g. 'three')"
            )
        if not isinstance(url, str) or not url:
            raise ValueError("ctx.add_js url must be a non-empty string")
        self._js_files[name] = url

    def add_css(self, css: str) -> None:
        """Inject a raw CSS block into the report iframe's document <head>.

        Use this for custom rules that need to reach widgets owning their
        own stylesheets in the outer document — Tabulator (table chrome),
        Bokeh DataTable, Plotly modebar. The "obvious" alternative,
        ``pn.pane.HTML('<style>…</style>')``, **does not work** for this:
        Panel sanitises HTML panes (entity-encodes ``<style>``), so the
        browser sees the rules as literal text inside a ``<div>``, not as
        a stylesheet. They never reach Tabulator's chrome.

        Plumbing: each call appends to a per-run list which
        ``panel_app._wrap_template`` then forwards onto
        ``template.config.raw_css``. That's a *per-template* (per-session)
        list — NOT the process-global ``pn.config.raw_css`` — so it
        doesn't leak between concurrent sessions.

        Only meaningful inside a report ``build(ctx)``; compute scripts
        stream into the chat, not into a Panel template.

        Example::

            def build(ctx):
                ctx.add_css('''
                    .tabulator { background: var(--voitta-surface); color: var(--voitta-text); }
                    .tabulator .tabulator-header { background: var(--voitta-surface-2); }
                ''')
                return pn.Column(
                    pn.widgets.Tabulator(df, sizing_mode="stretch_width"),
                )

        For the *common* themed-surface case, prefer ``ctx.apply_theme(layout, host=…)``
        — that covers Tabulator chrome (header, rows, hover, footer) and
        every other Panel surface automatically, no manual CSS needed.
        ``ctx.add_css`` is the escape hatch for rules ``apply_theme``
        doesn't reach.
        """

        if not isinstance(css, str):
            raise TypeError("ctx.add_css css must be a string")
        css = css.strip()
        if not css:
            return  # silently no-op on empty — convenient for conditional injection
        self._raw_css.append(css)

    def three_scene(
        self,
        scene_js: str,
        *,
        height: int = 480,
        version: str = "0.150.1",
        bg: str = "#1d1d1f",
    ) -> Any:
        """Ergonomic wrapper that wires Three.js for the common case.

        Returns a ``pn.pane.HTML`` containing an ``<iframe srcdoc="...">``
        (NOT a direct ``<div>`` — see docs/09-panel-threejs-reports.md
        for why: ``pn.pane.HTML`` reads ``clientWidth=0`` inside Bokeh's
        layout pass, killing canvas sizing). The iframe gets a real
        document context, real layout, and real ``window.load``.

        Inside the iframe, four locals are in scope when your
        ``scene_js`` runs: ``scene``, ``camera``, ``renderer``, ``THREE``.
        Add meshes/lights to ``scene``; an animation loop is already
        running and the canvas auto-resizes with the iframe.

        Default behaviour:
          * Soft ambient + directional lighting (override by adding your own)
          * Drag = orbit, wheel = zoom (no OrbitControls dep)
          * Camera starts at (0,0,5); set ``camera.position.set(x,y,z)``
            in ``scene_js`` to override

        The helper emits a ``<script type="module">`` with a Three.js
        importmap, so ``scene_js`` can use:

          * **Top-level `await`** — e.g. ``await new GLTFLoader().loadAsync(url)``.
          * **`import` of any `three/addons/*`** — ``GLTFLoader``,
            ``OrbitControls``, ``DRACOLoader``, etc. — via the bare
            specifier path. The importmap maps ``"three"`` →
            ``three.module.js`` and ``"three/addons/"`` →
            ``examples/jsm/``.

        Args:
          scene_js: user code, executed inside an async wrapper after
            THREE is imported.
          height: iframe height in px.
          version: three.js npm version (CDN: jsdelivr).
          bg: canvas/page background; defaults to portal-dark.

        Example::

            def build(ctx):
                return ctx.three_scene('''
                    const geom = new THREE.BoxGeometry(1, 1, 1);
                    const mat  = new THREE.MeshNormalMaterial();
                    scene.add(new THREE.Mesh(geom, mat));
                    camera.position.set(2, 1.5, 3);
                ''', height=520)

        GLB example::

            def build(ctx):
                import base64, pathlib
                rec = ctx.snapshot("py_abc")
                b64 = base64.b64encode(
                    (pathlib.Path(rec["path"]) / rec["meta"]["stored_name"]).read_bytes()
                ).decode()
                return ctx.three_scene(f'''
                    const {{GLTFLoader}} = await import('three/addons/loaders/GLTFLoader.js');
                    const bin = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
                    const url = URL.createObjectURL(new Blob([bin], {{type:'model/gltf-binary'}}));
                    const gltf = await new GLTFLoader().loadAsync(url);
                    scene.add(gltf.scene);
                    camera.position.set(5, 3, 8);
                ''', height=600)
        """

        import html
        import panel as pn

        # Module build + addons path. The importmap below wires both into
        # the iframe so user code can `import 'three'` / `import
        # 'three/addons/...'` like in any modern Three.js example. Don't
        # switch back to the UMD `three.min.js` build — addons (OrbitControls,
        # GLTFLoader, …) live only in the ES-module distribution.
        cdn_module = (
            f"https://cdn.jsdelivr.net/npm/three@{version}/build/three.module.js"
        )
        cdn_addons = (
            f"https://cdn.jsdelivr.net/npm/three@{version}/examples/jsm/"
        )
        # The full HTML document loaded into the iframe via srcdoc.
        # Keep this verbatim — it's all string-escaped below.
        viewer_doc = """<!doctype html>
<html><head>
<meta charset="utf-8">
<style>
  html,body { margin:0; height:100%; background:""" + bg + """; overflow:hidden; }
  #c { display:block; width:100%; height:100%; }
</style>
<script>
// Forward errors out of this nested srcdoc iframe to the outer Panel
// iframe via postMessage. `sandbox="allow-scripts"` means no same-origin
// access to the parent — but postMessage works across opaque origins.
// The outer shim's message listener (see backend/app/main.py
// `_panel_shim.js`) recognises `voitta_nested_error` and forwards into
// its standard render-error stream so get_report_render_errors picks it up.
(function(){
  function _forward(payload){
    try {
      window.parent.postMessage({
        type: 'voitta_nested_error',
        source: payload.source || 'window.error',
        message: String(payload.message || '').slice(0, 4000),
        stack: payload.stack ? String(payload.stack).slice(0, 6000) : null,
        url: payload.url || 'about:srcdoc',
        line: payload.line || null,
        col: payload.col || null
      }, '*');
    } catch (_) {}
  }
  window.addEventListener('error', function(e){
    var err = e && e.error;
    _forward({
      message: (err && err.message) || e.message || String(e),
      stack: err && err.stack,
      source: 'window.error',
      url: e.filename || 'about:srcdoc',
      line: e.lineno, col: e.colno
    });
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    var r = e && e.reason;
    _forward({
      message: (r && (r.message || String(r))) || 'unhandled rejection',
      stack: r && r.stack,
      source: 'unhandledrejection'
    });
  });
  // Also hook console.error so user code that catches and console.error's
  // (e.g. the user-scene_js try/catch wrapper below) still surfaces.
  var _origCE = console.error.bind(console);
  console.error = function(){
    try {
      var parts = [], stack = null;
      for (var i = 0; i < arguments.length; i++) {
        var a = arguments[i];
        if (a == null) { parts.push(''); continue; }
        if (typeof a === 'string') { parts.push(a); continue; }
        if (a.message) {
          var name = a.name || (a.constructor && a.constructor.name) || 'Error';
          parts.push(name + ': ' + a.message);
          if (!stack && a.stack) stack = a.stack;
          continue;
        }
        try { parts.push(String(a)); } catch (_) { parts.push('[unstringifiable]'); }
      }
      _forward({message: parts.join(' '), stack: stack, source: 'console.error'});
    } catch (_) {}
    return _origCE.apply(null, arguments);
  };
})();
</script>
<!-- ES module support for Three.js + addons (GLTFLoader, OrbitControls,
     etc). The importmap MUST come before any <script type="module"> that
     uses bare specifiers. Addons internally do `import 'three'`; without
     this map the browser throws "Failed to resolve module specifier". -->
<script type="importmap">
{
  "imports": {
    "three": \"""" + cdn_module + """\",
    "three/addons/": \"""" + cdn_addons + """\"
  }
}
</script>
</head><body>
<canvas id="c"></canvas>
<script type="module">
// Module script — top-level `await` is legal here. THREE is imported at
// module scope and remains in scope for user `scene_js` inside the async
// wrapper below.
import * as THREE from 'three';

  const canvas = document.getElementById("c");
  const scene  = new THREE.Scene();
  scene.background = new THREE.Color(""" + repr(bg) + """);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);

  // Initial size — may be 0 if the iframe hasn't been laid out yet.
  // ResizeObserver below will correct it on the first real layout tick.
  let w = canvas.clientWidth | 0, h = canvas.clientHeight | 0;
  // Guard against w/0 → Infinity aspect: clamp h to 1 for the initial
  // projection matrix. Subsequent resizes recompute properly.
  const camera = new THREE.PerspectiveCamera(60, w / Math.max(h, 1), 0.1, 1000);
  if (w > 0 && h > 0) renderer.setSize(w, h, false);

  // Default lighting — user code can add more.
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const dir = new THREE.DirectionalLight(0xffffff, 0.85);
  dir.position.set(2, 4, 3);
  scene.add(dir);

  // Drag-to-rotate + wheel-zoom (no OrbitControls dep).
  //
  // Use setPointerCapture on pointerdown so the canvas keeps receiving
  // move/up events even when the pointer leaves its box mid-drag.
  // Without this, dragging fast off the canvas freezes rotation until
  // the pointer returns inside.
  let isDown = false, lastX = 0, lastY = 0;
  const target = new THREE.Vector3(0, 0, 0);
  let yaw = 0, pitch = 0, radius = 5;
  function applyCam() {
    camera.position.x = target.x + radius * Math.sin(yaw) * Math.cos(pitch);
    camera.position.y = target.y + radius * Math.sin(pitch);
    camera.position.z = target.z + radius * Math.cos(yaw) * Math.cos(pitch);
    camera.lookAt(target);
  }
  canvas.addEventListener("pointerdown", (e) => {
    isDown = true; lastX = e.clientX; lastY = e.clientY;
    try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
  });
  canvas.addEventListener("pointerup", (e) => {
    isDown = false;
    try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
  });
  canvas.addEventListener("pointercancel", (e) => {
    isDown = false;
    try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
  });
  canvas.addEventListener("pointermove",  (e) => {
    if (!isDown) return;
    // Sign convention matches OrbitControls / Blender / SolidWorks /
    // Fusion: drag-right rotates the camera CCW around world-Y (looking
    // from +Y), so the scene visually flows to the RIGHT under the
    // cursor — cursor "leads" the world motion. The opposite sign feels
    // inverted across every starting viewpoint, not just below the
    // equator.
    yaw   -= (e.clientX - lastX) * 0.005;
    pitch += (e.clientY - lastY) * 0.005;
    pitch = Math.max(-1.5, Math.min(1.5, pitch));
    lastX = e.clientX; lastY = e.clientY;
    applyCam();
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    radius = Math.max(0.1, Math.min(500, radius * (1 + e.deltaY * 0.001)));
    applyCam();
  }, { passive: false });

  // ── user scene_js ─────────────────────────────────────────────
  // Wrapped in an async IIFE so users can:
  //   * use top-level `await` freely (e.g. `await loader.loadAsync(url)`)
  //   * use dynamic `import('three/addons/...')` resolved via importmap
  //   * any thrown error gets caught and surfaces via the error
  //     forwarder, but does not nuke the render loop below
  // We AWAIT the wrapper so the camera-aim heuristic below runs AFTER
  // user code has had a chance to set `camera.position` (including via
  // an async load that finishes inside the IIFE).
  try {
    await (async () => {
__USER_SCENE_JS__
    })();
  } catch (err) {
    console.error("voitta three_scene user code threw:", err);
  }
  // ─────────────────────────────────────────────────────────────

  // If the user moved the camera, derive yaw/pitch/radius from it so
  // drag-rotate keeps working from there. Otherwise default to (0,0,5).
  //
  // Crucially: auto-aim the camera at `target` (the drag-orbit centre
  // at the origin). `camera.position.set(...)` only PLACES the camera;
  // it does NOT change where it looks. Without this lookAt(), a
  // user-set position keeps the default forward of (0, 0, -1) and the
  // geometry renders crammed into the bottom-left of the viewport
  // (NDC outside [-1, 1]^2 on both axes). This is the #1 footgun new
  // users hit with raw Three.js; the helper handles it.
  //
  // If you genuinely want the camera looking elsewhere, you cannot use
  // this helper as-is — drag-rotate is hard-coded to orbit (0, 0, 0).
  // See docs/09-panel-threejs-reports.md for the custom-iframe pattern.
  if (camera.position.lengthSq() === 0) {
    radius = 5; applyCam();
  } else {
    const p = camera.position;
    radius = p.length();
    pitch  = Math.asin(p.y / (radius || 1));
    yaw    = Math.atan2(p.x, p.z);
    camera.lookAt(target);
  }

  // ResizeObserver on the document body. This is the only reliable
  // way to track the iframe's effective rendering size:
  //   - `window.resize` is not consistently fired when the OUTER
  //     iframe element is resized via CSS (Panel/Bokeh GridSpec
  //     layout passes do exactly that on first paint).
  //   - `window.innerWidth` reads 0 during the very first iframe load
  //     before the iframe has been laid out.
  // ResizeObserver fires on `observe()` AND on every subsequent
  // layout shift, so it covers both the initial sizing and any later
  // reflow (window resize, drawer-width drag, etc.).
  function applyViewport(nw, nh) {
    if (nw <= 0 || nh <= 0) return;
    w = nw; h = nh;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver((entries) => {
    for (const ent of entries) {
      // contentBoxSize is the modern, accurate path; fall back to
      // contentRect for older browsers (Safari < 15.4).
      let nw = 0, nh = 0;
      const cbs = ent.contentBoxSize;
      if (cbs && cbs.length) {
        const size = Array.isArray(cbs) ? cbs[0] : cbs;
        nw = size.inlineSize; nh = size.blockSize;
      } else {
        nw = ent.contentRect.width; nh = ent.contentRect.height;
      }
      applyViewport(nw | 0, nh | 0);
    }
  });
  ro.observe(document.body);

  // Render loop. Skip when the canvas is 0-size so we don't push
  // garbage frames into a zero-pixel buffer during the brief window
  // between iframe load and the first ResizeObserver tick.
  function animate() {
    requestAnimationFrame(animate);
    if (w > 0 && h > 0) renderer.render(scene, camera);
  }
  animate();
</script>
</body></html>"""

        viewer_doc = viewer_doc.replace("__USER_SCENE_JS__", scene_js)
        wrapper = (
            f'<iframe srcdoc="{html.escape(viewer_doc, quote=True)}" '
            f'style="width:100%;height:{height}px;border:0;display:block;" '
            f'sandbox="allow-scripts"></iframe>'
        )
        return pn.pane.HTML(wrapper, sizing_mode="stretch_width")

    def get_theme(self, host: str | None = None) -> dict[str, Any]:
        """Return the active theme bundle for ``host``.

        Python-side equivalent of the ``get_active_theme`` LLM tool —
        gives the same dict shape (``palette``, ``raw_tokens``,
        ``is_dark``, ``css_snippet``, etc.) without a tool round-trip.

        Use this inside ``build(ctx)`` to colour charts and surfaces:

            theme = ctx.get_theme(host="enterprise.voitta.ai")
            p = theme["palette"]
            plt.rcParams["figure.facecolor"] = p["surfaces"]["bg"]

        Omit ``host`` for the bare Voitta defaults. The hostname is
        normally available in the ambient context the orchestrator
        injects on the user message.
        """
        from app.tools.domain import theme as _theme

        return _theme.resolve_theme(host)

    def theme_css(
        self,
        host: str | None = None,
        *,
        theme: dict[str, Any] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> str:
        """Return the theme as a plain CSS string.

        This is the **flat replacement** for the old hidden-skin-object
        pipeline. There is no magic stamping, no per-layout attribute,
        no second-pass server-side transform. You get a CSS string;
        you decide where to put it:

        * Outer-document widgets (Markdown, Card, plain panes,
          template chrome) → ``ctx.add_css(css)``.

        * Shadow-DOM widgets (``pn.widgets.Tabulator``, Bokeh
          ``DataTable``, some input widgets) → pass via the widget's
          ``stylesheets=[css]`` kwarg. Outer-doc CSS can't pierce
          Bokeh's per-component shadow roots, so this is the *only*
          channel that themes their chrome.

        Same string works on both surfaces — the variable block uses
        ``:root, :host`` so it binds in either context.

        Example::

            def build(ctx):
                css = ctx.theme_css(host="enterprise.voitta.ai")
                ctx.add_css(css)                                 # Markdown/Card/etc.
                table = pn.widgets.Tabulator(df,
                    sizing_mode="stretch_width",
                    layout="fit_columns",
                    stylesheets=[css],                           # Tabulator shadow root
                )
                return pn.Column(pn.pane.Markdown("# Title"), table)

        Pass either ``host`` (and we resolve the active theme for it)
        or ``theme`` (a dict from a prior ``ctx.get_theme(...)``). If
        both are passed, ``theme`` wins. ``overrides`` is a dict of
        ``{"--voitta-…": value}`` applied on top of the resolved theme.

        Returns ``""`` (empty string) if no tokens resolve — easy to
        guard with ``if css: ctx.add_css(css)``.
        """
        if theme is None:
            theme = self.get_theme(host)
        raw = theme.get("raw_tokens") if isinstance(theme, dict) else None
        if not isinstance(raw, dict) or not raw:
            return ""
        merged = {
            k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        is_dark = bool(theme.get("is_dark", False))
        if overrides:
            if not isinstance(overrides, dict):
                raise TypeError(
                    "ctx.theme_css overrides must be a dict of "
                    "{token_name: value}; got " + type(overrides).__name__
                )
            for name, value in overrides.items():
                if not isinstance(name, str) or not name.startswith("--"):
                    raise ValueError(
                        f"ctx.theme_css override key {name!r} must be a "
                        "CSS custom-property name starting with '--' "
                        "(e.g. '--voitta-accent'). Look up valid names "
                        "in ctx.get_theme(host=…)['raw_tokens']."
                    )
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"ctx.theme_css override value for {name!r} must "
                        "be a non-empty string."
                    )
                merged[name] = value
            # Recompute is_dark if the override changed the surface bg —
            # build_theme_css uses this for ``color-scheme: light|dark``.
            from app.tools.domain.theme import looks_dark
            if "--voitta-bg" in overrides:
                is_dark = looks_dark(merged["--voitta-bg"])
        # Late-import to avoid a startup cycle (panel_app imports scripts).
        from app.services.panel_app import build_theme_css
        return build_theme_css(merged, is_dark)

    def apply_theme(
        self,
        layout: Any,
        host: str | None = None,
        theme: dict[str, Any] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> Any:
        """Convenience: compute theme CSS and inject it into both the
        outer document AND every shadow-DOM widget in ``layout``.

        Equivalent to the explicit form::

            css = ctx.theme_css(host=...)
            ctx.add_css(css)
            # … then for every Tabulator/DataTable/etc. in layout:
            widget.stylesheets = [*widget.stylesheets, css]

        We walk the layout once after the user finishes building it
        and attach ``stylesheets=[css]`` to widget classes whose
        chrome lives behind a shadow boundary (``pn.widgets.Tabulator``,
        Bokeh ``DataTable``). Outer-document CSS reaches everything
        else via :func:`add_css`.

        Returns ``layout`` unchanged so you can chain at the end of
        ``build``::

            def build(ctx):
                grid = pn.GridSpec(...)
                grid[0, 0] = ...
                return ctx.apply_theme(grid, host="enterprise.voitta.ai")

        If you want explicit control (e.g. attach to one widget but
        not another), skip this helper and call ``ctx.theme_css`` +
        ``ctx.add_css`` directly. That's the unsugared dataflow —
        this method is just a 4-line wrapper.

        What this does NOT do:
          * It does not touch the *contents* of the panes — your
            matplotlib figure colours, Plotly chart palette, Three.js
            scene materials remain your responsibility. Read
            ``ctx.get_theme(host=…)["palette"]`` and apply via
            ``rcParams`` / ``update_layout`` / ``ctx.three_scene(bg=…)``.
          * It does not override ``pn.pane.HTML`` content you wrote
            with explicit inline ``style="background: …"`` attributes.
        """
        css = self.theme_css(host=host, theme=theme, overrides=overrides)
        if not css:
            return layout
        # Outer-document injection — Markdown, Card, template chrome,
        # iframe body. Same channel any ctx.add_css call uses.
        self.add_css(css)
        # Shadow-root injection — walk the layout once and add the CSS
        # to each shadow-DOM widget's ``stylesheets`` array. Without
        # this the widget's per-component shadow tree keeps Tabulator's
        # default white theme regardless of how much CSS we shoved into
        # the outer document.
        _attach_shadow_stylesheets(layout, css)
        return layout

    def log(self, *args: Any) -> None:
        if len(self._log_lines) >= MAX_LOG_LINES:
            return
        msg = " ".join(self._stringify(a) for a in args)[:MAX_LOG_LINE_BYTES]
        self._log_lines.append(msg)

    @staticmethod
    def _stringify(a: Any) -> str:
        if isinstance(a, str):
            return a
        try:
            return json.dumps(a, default=str, ensure_ascii=False)
        except Exception:
            return str(a)


# Bokeh widget classes whose chrome lives behind a shadow root that
# outer-document CSS cannot reach. ``ctx.apply_theme`` walks the
# layout tree and appends the theme CSS to each instance's
# ``stylesheets`` array; that's the only channel Bokeh has for
# styling inside the shadow root.
#
# Resolved lazily because Panel imports are expensive at module load
# and we don't want to force-import every widget family. Missing
# classes (an older Panel that doesn't ship a name) are simply
# skipped — the user's table won't be themed, but build() still runs.
def _shadow_dom_widget_classes() -> tuple[type, ...]:
    """Bokeh components whose chrome lives behind a per-instance
    shadow root.  Outer-document CSS doesn't pierce these, so the
    theme stylesheet has to be attached to each instance via
    ``stylesheets=[…]``.

    Includes BOTH widgets AND content panes — Panel renders
    ``pn.pane.Markdown`` / ``pn.pane.HTML`` inside a Bokeh
    ``ReactiveHTML`` component that has its own ``stylesheets``
    array AND consumes Panel's ``--panel-on-background-color`` chain
    in its bundled markdown.css. Without theme CSS in the shadow
    root the body text inherits the user-agent default of black —
    which on a dark theme is invisible.
    """
    import panel as pn

    out: list[type] = []
    for name in (
        # Tables — the headline offenders the user first hit.
        "Tabulator",
        # Bokeh's DataTable widget. Same shadow story as Tabulator.
        "DataFrame",
        # Date / range pickers — Flatpickr inside shadow root.
        "DatePicker",
        "DatetimePicker",
        "DateRangePicker",
        "DatetimeRangePicker",
    ):
        cls = getattr(pn.widgets, name, None)
        if cls is not None:
            out.append(cls)
    # Content panes — same shadow story. ``Markdown``, ``HTML``, and
    # ``Str`` all flow through Bokeh's MarkupView which wraps content
    # in a per-instance shadow root.  Without CSS attached here,
    # paragraph text in dark themes renders black-on-dark because
    # Panel's bundled markdown.css resolves ``color`` through a
    # ``--panel-on-background-color`` chain we now bridge from the
    # ``--voitta-text`` token (see build_theme_css).
    for name in ("Markdown", "HTML", "Str"):
        cls = getattr(pn.pane, name, None)
        if cls is not None:
            out.append(cls)
    return tuple(out)


def _attach_shadow_stylesheets(layout: Any, css: str) -> None:
    """Walk ``layout`` recursively; append ``css`` to the
    ``stylesheets`` list of every shadow-DOM widget we find.

    Why this exists: ``template.config.raw_css`` (and equivalently
    ``ctx.add_css``) injects into the iframe's outer-document
    ``<head>``. That cascade reaches Markdown, Card, and most plain
    Panel surfaces — but stops at Bokeh's per-widget shadow roots.
    Tabulator, DataTable, and the Flatpickr-backed date widgets each
    live in their own shadow tree with their own ``stylesheets``
    array; rules need to land there too or those widgets keep their
    default light theme.

    Mutates ``stylesheets`` in place (append-only — we never
    overwrite the widget's existing entries, which include Panel's
    own bundled theme + the Tabulator/Flatpickr CSS). If a widget
    has no ``stylesheets`` attribute (some Bokeh primitives don't),
    it's silently skipped.
    """
    if css is None or not css.strip():
        return
    shadow_types = _shadow_dom_widget_classes()
    if not shadow_types:
        return

    seen: set[int] = set()

    def walk(obj: Any) -> None:
        oid = id(obj)
        if oid in seen:
            return
        seen.add(oid)
        if isinstance(obj, shadow_types):
            existing = list(getattr(obj, "stylesheets", []) or [])
            # Idempotent: don't double-append on re-themes (someone
            # calling apply_theme twice during a build, or a future
            # incremental refresh).
            if css not in existing:
                existing.append(css)
                try:
                    obj.stylesheets = existing
                except Exception:
                    # Some Bokeh objects have read-only attrs in odd
                    # states (e.g. during validation). Best-effort.
                    pass
        # Recurse into Panel layouts/Tabs/GridSpec/Cards via the
        # ``objects`` iterable; Bokeh layouts via ``children``.
        for attr in ("objects", "children"):
            kids = getattr(obj, attr, None)
            if kids is None:
                continue
            # GridSpec stores dict values; everything else is a list.
            iterable = kids.values() if isinstance(kids, dict) else kids
            try:
                for child in iterable:
                    walk(child)
            except TypeError:
                # Not iterable — leaf.
                continue

    walk(layout)


# ---- execution ------------------------------------------------------------


def _exec_script(code: str, source_label: str) -> dict:
    """Compile + exec the script body in a fresh namespace. The script
    typically defines top-level functions (``run`` for compute,
    ``build`` for reports). Raises ``ScriptError`` with a truncated
    traceback if the script body itself raises."""

    ns: dict[str, Any] = {"__name__": "__voitta_script__"}
    try:
        exec(compile(code, source_label, "exec"), ns)  # noqa: S102
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])
    return ns


def _run_compute_blocking(code: str, ctx: ScriptContext, args: Any) -> Any:
    """Run a compute script in the calling thread. Returns the script's
    return value (or None). Raises ``ScriptError`` on script exceptions."""

    ns = _exec_script(code, "<compute>")
    fn = ns.get("run")
    if not callable(fn):
        raise ScriptError("compute script must define a top-level `run(ctx, args=None)` function")
    try:
        # Try the (ctx, args) form first; fall back to (ctx,) for
        # scripts that don't accept args.
        try:
            return fn(ctx, args)
        except TypeError:
            if args is None:
                return fn(ctx)
            raise
    except ScriptError:
        raise
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])


def _run_report_blocking(code: str, ctx: ScriptContext) -> Any:
    """Run a report script. Returns its Panel layout (or whatever
    ``build(ctx)`` returns). Raises ``ScriptError`` on script
    exceptions."""

    ns = _exec_script(code, "<report>")
    fn = ns.get("build")
    if not callable(fn):
        raise ScriptError("report script must define a top-level `build(ctx)` function")
    try:
        return fn(ctx)
    except ScriptError:
        raise
    except Exception:
        raise ScriptError(traceback.format_exc()[-2000:])


# ---- public: compute scripts ----------------------------------------------


async def run_compute(name: str, code: str, args: Any = None, *, timeout_s: float | None = None) -> dict:
    """Persist + execute a compute script. Returns:

        {
          ok, name, run_id, result, error?, items, log_lines, elapsed_s
        }
    """

    slug = slugify(name)
    code_path, _ = _persist("compute", slug, code)
    run_id = secrets.token_hex(4)
    ctx = ScriptContext(run_id, kind="compute", slug=slug)

    timeout = max(1.0, min(timeout_s or COMPUTE_TIMEOUT_S, COMPUTE_TIMEOUT_MAX_S))
    started = time.time()

    ok = True
    error: str | None = None
    result: Any = None
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_compute_blocking, code, ctx, args),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        ok = False
        error = f"timeout after {timeout:.1f}s"
    except ScriptError as exc:
        ok = False
        error = str(exc)
    except Exception as exc:  # pragma: no cover — host-level failure
        ok = False
        error = f"{type(exc).__name__}: {exc}"

    elapsed_s = round(time.time() - started, 2)
    _update_run_meta(
        "compute", slug, ok=ok, run_id=run_id, elapsed_s=elapsed_s, error=error
    )

    return {
        "ok": ok,
        "name": slug,
        "run_id": run_id,
        "result": result,
        "error": error,
        "items": ctx._items,  # surfaced as rich blocks by the chat handler
        "log_lines": ctx._log_lines,
        "elapsed_s": elapsed_s,
        "code_path": str(code_path),
    }


def _list_kind(kind: str) -> list[dict]:
    base = _KIND_DIRS.get(kind)
    if base is None:
        raise ValueError(f"invalid script kind {kind!r}")
    _ensure_dirs()
    out: list[dict] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "code.py").exists():
            continue
        meta = _read_meta(d / "meta.json")
        out.append({"name": d.name, **meta})
    return out


def list_compute() -> list[dict]:
    return _list_kind("compute")


def get_compute(name: str) -> dict | None:
    slug = slugify(name)
    code_path = _code_path("compute", slug)
    if not code_path.exists():
        return None
    return {
        "name": slug,
        "code": code_path.read_text(),
        "meta": _read_meta(_meta_path("compute", slug)),
    }


def delete_compute(name: str) -> bool:
    slug = slugify(name)
    d = _script_dir("compute", slug)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# ---- public: report scripts -----------------------------------------------


def define_report(name: str, code: str) -> dict:
    """Persist a report script. Returns ``{name, code_path, meta}``. The
    rendering happens at GET-time on /api/reports/{name}."""

    slug = slugify(name)
    # Reject bodies that don't define `build` early (cheap parse-only check).
    try:
        compile(code, "<report-validation>", "exec")
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc.msg} at line {exc.lineno}")
    code_path, meta = _persist("reports", slug, code)
    return {"name": slug, "code_path": str(code_path), "meta": meta}


def list_reports() -> list[dict]:
    return _list_kind("reports")


def get_report_script(name: str) -> dict | None:
    slug = slugify(name)
    code_path = _code_path("reports", slug)
    if not code_path.exists():
        return None
    return {
        "name": slug,
        "code": code_path.read_text(),
        "meta": _read_meta(_meta_path("reports", slug)),
    }


def delete_report(name: str) -> bool:
    slug = slugify(name)
    d = _script_dir("reports", slug)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# ---- public: incremental edits --------------------------------------------


def edit_script(kind: str, name: str, edits: list[dict]) -> dict:
    """Apply a sequence of search-replace edits to a stored script.

    Each edit is ``{"find": str, "replace": str, "replace_all": bool}``
    (default ``replace_all=False``). Same semantics as Claude Code's
    Edit tool:

      • ``find`` must occur in the current code (after preceding edits).
      • If ``replace_all`` is False, ``find`` must occur exactly once;
        a multi-match is rejected so the model surfaces a non-unique
        anchor early instead of silently editing the wrong site.
      • Edits apply in order; later edits see earlier edits' results.

    Atomicity: if ANY edit fails (not found / non-unique / final syntax
    error) we don't write anything — the script on disk stays as it was.
    The model retries with better anchors instead of debugging a
    half-applied state.

    Returns ``{name, code_path, applied: [{find, replace, count}]}`` on
    success, raises ``ValueError`` with a human-readable message on
    failure.
    """

    if kind not in _KIND_DIRS:
        raise ValueError(f"invalid kind {kind!r}")
    if not edits:
        raise ValueError("edits list is empty")

    slug = slugify(name)
    code_path = _code_path(kind, slug)
    if not code_path.exists():
        raise ValueError(f"no {kind} script named {name!r}")

    code = code_path.read_text()
    applied: list[dict] = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edit #{i + 1}: expected object, got {type(edit).__name__}")
        find = edit.get("find")
        replace = edit.get("replace")
        replace_all = bool(edit.get("replace_all", False))
        if not isinstance(find, str) or find == "":
            raise ValueError(f"edit #{i + 1}: 'find' must be a non-empty string")
        if not isinstance(replace, str):
            raise ValueError(f"edit #{i + 1}: 'replace' must be a string")
        count = code.count(find)
        if count == 0:
            raise ValueError(f"edit #{i + 1}: 'find' string not present in script")
        if count > 1 and not replace_all:
            raise ValueError(
                f"edit #{i + 1}: 'find' matches {count} sites; "
                "set replace_all=true OR add more surrounding context to make it unique"
            )
        code = code.replace(find, replace) if replace_all else code.replace(find, replace, 1)
        applied.append({"find": find, "replace": replace, "count": count if replace_all else 1})

    # Reject syntactically-broken results before writing — same guard as
    # define_report. Compute scripts go through the same check for
    # symmetry; the cost is negligible.
    try:
        compile(code, f"<{kind}-edit-validation>", "exec")
    except SyntaxError as exc:
        raise ValueError(
            f"edits would leave script with a syntax error: "
            f"{exc.msg} at line {exc.lineno} (no changes written)"
        )

    code_path, _ = _persist(kind, slug, code)
    return {"name": slug, "code_path": str(code_path), "applied": applied}


def report_script_layout(report_id: str) -> Any | None:
    """Run a stored report script and return its Panel layout.

    Returns ``None`` if no script with slugified ``report_id`` exists —
    the caller (typically ``app.services.panel_app.panel_factory``)
    falls back to the mock layout in that case.

    Synchronous on purpose: it's called inside a Bokeh session document,
    where async wait isn't useful and ``run_in_executor`` would just add
    a layer. Errors raise ``ScriptError`` so the factory can render an
    error layout instead of leaking a 500.
    """

    try:
        slug = slugify(report_id)
    except ValueError:
        return None
    code_path = _code_path("reports", slug)
    if not code_path.exists():
        return None
    code = code_path.read_text()
    run_id = secrets.token_hex(4)
    ctx = ScriptContext(run_id, kind="reports", slug=slug)
    started = time.time()
    ok = True
    error: str | None = None
    layout: Any = None
    try:
        layout = _run_report_blocking(code, ctx)
    except ScriptError as exc:
        ok = False
        error = str(exc)
        raise
    except Exception as exc:  # pragma: no cover
        ok = False
        error = f"{type(exc).__name__}: {exc}"
        raise ScriptError(error)
    finally:
        elapsed_s = round(time.time() - started, 2)
        _update_run_meta(
            "reports", slug, ok=ok, run_id=run_id, elapsed_s=elapsed_s, error=error
        )
    # Forward any ctx.add_js() registrations onto the layout so the
    # template wrapper can merge them into ``js_files`` (see
    # app.services.panel_app._wrap_template).
    if layout is not None and ctx._js_files:
        try:
            layout._voitta_extra_js_files = dict(ctx._js_files)
        except (AttributeError, TypeError):
            # Some Bokeh objects refuse arbitrary attributes; that's OK,
            # the user just doesn't get the extra scripts.
            pass
    # Same plumbing for ctx.add_css() / ctx.apply_theme() outer-doc
    # CSS — stamped under a separate attr so _wrap_template can route
    # into ``template.config.raw_css``. apply_theme additionally walks
    # the layout itself to attach shadow-DOM stylesheets at build
    # time — see ScriptContext.apply_theme.
    if layout is not None and ctx._raw_css:
        try:
            layout._voitta_extra_raw_css = list(ctx._raw_css)
        except (AttributeError, TypeError):
            pass
    return layout


# Cap the smoke-test error message so the LLM doesn't burn its context
# window on a 50-frame Bokeh/Panel traceback. Tracebacks already get
# truncated to 2000 bytes by ``_run_report_blocking``; we tighten that
# further for the tool-result path. The tail (where the actual exception
# is) is the useful part — keep the last N bytes.
SMOKE_ERROR_MAX_BYTES = 1500


def smoke_test_report(name: str) -> str | None:
    """Run a stored report's ``build(ctx)`` once and return any error.

    Returns ``None`` on success, otherwise a truncated error message
    suitable for surfacing back to the LLM via tool result. Used by
    ``define_report`` and ``edit_report_script`` so runtime errors land
    at the moment the model has the most context to fix them — instead
    of after the user opens the iframe and hits a red error page.

    The script's run metadata is updated either way (this counts as a
    real run via ``report_script_layout``), so subsequent ``list_reports``
    output reflects the smoke result.
    """

    try:
        report_script_layout(name)
        return None
    except ScriptError as exc:
        msg = str(exc)
        if len(msg) > SMOKE_ERROR_MAX_BYTES:
            msg = "…[truncated]…\n" + msg[-SMOKE_ERROR_MAX_BYTES:]
        return msg
    except Exception as exc:  # pragma: no cover — defensive
        return f"{type(exc).__name__}: {str(exc)[:SMOKE_ERROR_MAX_BYTES]}"


# ---- public: cleanup ------------------------------------------------------


def clear_script_output() -> dict:
    """Delete every ``runs/<run_id>/`` directory under every script.
    Doesn't touch script source or meta. Returns
    ``{freed_bytes, removed_runs}``."""

    freed = 0
    removed = 0
    for base in (SCRIPTS_COMPUTE, SCRIPTS_REPORTS):
        if not base.exists():
            continue
        for script_dir in base.iterdir():
            if not script_dir.is_dir():
                continue
            runs = script_dir / "runs"
            if not runs.is_dir():
                continue
            for run_dir in list(runs.iterdir()):
                if not run_dir.is_dir():
                    continue
                for f in run_dir.rglob("*"):
                    if f.is_file():
                        freed += f.stat().st_size
                shutil.rmtree(run_dir)
                removed += 1
    return {"freed_bytes": freed, "removed_runs": removed}
