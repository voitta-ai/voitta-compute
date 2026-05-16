"""FastAPI application — entrypoint for `uvicorn app.main:app`.

Mounts the chat router under `/api`, the bridge router at the root (so
URLs match `voitta-bookmarklet`), and serves the built widget bundle from
`frontend/dist/widget.js` for the bookmarklet to load.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes.chat import router as chat_router
from app.routes.cli import router as cli_router
from app.routes.plugins import router as plugins_router
from app.routes.providers import router as providers_router
from app.routes.tools import router as tools_router


# Mirror app.config.PROJECT_ROOT so packaged (.app) and dev modes
# resolve resources to the same place. config picks up the env-var
# override `VOITTA_PROJECT_ROOT` set by the desktop launcher.
from app.config import PROJECT_ROOT  # noqa: E402

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


app = FastAPI(title="Voitta Bookmarklet Backend", version="0.1.0")

# CORS. ``allow_credentials=True`` is required so cookies set by
# /api/auth/login round-trip on subsequent fetches from the host page
# (drive.google.com etc → 127.0.0.1:12358 is cross-origin). The CORS
# spec forbids ``*`` together with credentials, so we echo the request
# Origin via a regex match instead — semantically equivalent for our
# threat model since auth is the cookie, not the origin.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ---- auth gate -------------------------------------------------------------
#
# When LOCALHOST_MODE is on (the default for the .app), every request
# passes. Otherwise we require the request to carry the auth cookie set
# by ``POST /api/auth/login``, OR the route to be in the open-allowlist
# (widget bootstrap + the auth endpoints themselves).
#
# Allowlist routes:
#   GET /widget.js              bookmarklet has to load before login
#   GET /widget.js.map          dev-mode source map
#   POST /api/auth/login        the login endpoint itself
#   GET  /api/auth/status       cheap "are we authed?" probe
#   POST /api/auth/logout       not auth-gated by spec (anyone can quit)
#   GET  /healthz               opt-in liveness check
#
# Everything else (chat, tools, RAG, Drive, reports, scripts) is gated.

_AUTH_OPEN_ROUTES = frozenset({
    "GET /widget.js",
    "GET /widget.js.map",
    "POST /api/auth/login",
    "GET /api/auth/status",
    "POST /api/auth/logout",
    "GET /healthz",
})


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    from app import config as _cfg

    # Always pass in localhost mode — current production .app default.
    if _cfg.LOCALHOST_MODE:
        return await call_next(request)

    # CORS preflight — the browser hasn't attached cookies yet, and
    # blocking it kills every cross-origin call. The CORSMiddleware
    # above answers it; let the request through.
    if request.method == "OPTIONS":
        return await call_next(request)

    key = f"{request.method} {request.url.path}"
    if key in _AUTH_OPEN_ROUTES:
        return await call_next(request)
    # Plugin bootstrap routes are open — theme.css served by name (/api/plugin/{name}/theme.css)
    if request.method == "GET" and request.url.path.startswith("/api/plugin"):
        return await call_next(request)

    cookie = request.cookies.get(_cfg.AUTH_COOKIE_NAME)
    if cookie and cookie == _cfg.API_KEY:
        return await call_next(request)

    # Bearer header for non-browser callers (curl / sdk).
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        if auth_header[7:].strip() == _cfg.API_KEY:
            return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={
            "error": "unauthenticated",
            "message": (
                "This Voitta backend is running in LAN mode and requires "
                "an API key. POST /api/auth/login with body "
                "{\"api_key\": \"...\"} to get a session cookie."
            ),
        },
    )


# Chrome's Private Network Access (PNA) gate. When a public origin (e.g.
# drive.google.com) talks to a private/loopback address (e.g. 127.0.0.1),
# Chrome sends a CORS preflight that includes
# ``Access-Control-Request-Private-Network: true``. The local server has
# to consent by echoing ``Access-Control-Allow-Private-Network: true``
# on the preflight response — otherwise the actual request fails with
# "Permission was denied for this request to access the `loopback`
# address space." FastAPI's CORS middleware doesn't emit that header on
# its own, so we patch every response (preflight or not — extra header
# on non-preflight responses is harmless).
@app.middleware("http")
async def _allow_private_network(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


app.include_router(chat_router, prefix="/api")
app.include_router(tools_router)
app.include_router(providers_router)
# Plugin diagnostics + MCP refresh control. Routes already carry the
# /api/plugins prefix internally, so we mount without an extra prefix.
app.include_router(plugins_router)


@app.on_event("startup")
async def _bootstrap_plugins_and_mcp() -> None:
    """One-shot plugin discovery + MCP connector probe at boot.

    Two phases, in order:

    1. ``discover_plugins()`` walks ``plugins/*``, imports each
       plugin's Python module so its ``ToolSpec`` registrations land
       on the global registry, AND records ``mcp_servers`` manifest
       entries as MCP connector declarations. This used to run as a
       module-load side effect in ``app.tools.providers``; it's
       deferred to startup now to avoid a circular import (the
       discovery path needs ``app.services.mcp`` which back-loads
       ``app.tools.__init__``).

    2. ``refresh_all()`` opens one streamable-http connection per
       connector, lists remote tools, synthesises ``ToolSpec``s.
       Per the design contract this is the only automatic probe —
       further refreshes come from the Settings "Refresh tool list"
       button.

    Failures are absorbed so a down MCP server doesn't block startup;
    affected connectors show ``unreachable`` in the Settings panel.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        from app.tools.providers import discover_plugins

        discover_plugins()
    except Exception:
        # Plugin discovery failing at startup is unusual but shouldn't
        # take the backend down — core tools still work, just no
        # plugin-contributed ones.
        log.exception("plugin discovery raised")

    try:
        from app.services.mcp import refresh_all as _refresh_mcp

        await _refresh_mcp()
    except Exception:
        log.exception("MCP startup refresh raised")

    # Bind the main event loop into ensure_local so that script-side
    # ``ctx.ensure_local(ref)`` calls — which run in a thread-pool
    # executor — can dispatch async resolvers back to this loop via
    # ``run_coroutine_threadsafe``. Also import the resolvers package
    # so its modules register themselves with the ensure_local
    # registry (side-effect imports).
    try:
        import asyncio
        from app.services import ensure_local as _ensure_local
        from app.services import resolvers as _resolvers  # noqa: F401

        _ensure_local.bind_loop(asyncio.get_running_loop())
    except Exception:
        log.exception("ensure_local startup wiring raised")
# Localhost-only back-channel for external automation (Claude Code,
# scripts). NOT exposed to the in-pane chat LLM. See app/routes/cli.py.
app.include_router(cli_router)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        # Provider keys live in browser localStorage (set via the in-pane
        # Settings panel). The backend is intentionally key-less.
        "providers_supported": ["anthropic", "openai", "gemini"],
        "max_tool_iterations_ceiling": settings.max_tool_iterations_ceiling,
    }


def _plugin_for_host(host: str) -> dict | None:
    """First plugin whose host_patterns matches — back-compat shim.

    Delegates to :func:`plugins_for_host`. Used by the single-plugin
    endpoints (``GET /api/plugin``, theme-css) where today's data model
    really is one-to-one. Multi-plugin callers should use
    :func:`plugins_for_host` directly.
    """
    from app.tools.providers import plugins_for_host

    matches = plugins_for_host(host)
    return matches[0] if matches else None


@app.get("/api/plugin")
async def plugin_for_host(host: str) -> JSONResponse:
    plugin = _plugin_for_host(host)
    if plugin is None:
        raise HTTPException(status_code=404, detail="No plugin matched this host")
    manifest = plugin["manifest"]
    name = plugin["name"]
    agent_name = manifest.get("agent_name") or name.title()
    theme_path = Path(plugin["path"]) / "theme.css"
    result: dict = {"name": name, "agent_name": agent_name}
    if theme_path.exists():
        result["theme_url"] = f"/api/plugin/{name}/theme.css"
    raw_layout = manifest.get("default_layout", "")
    if raw_layout in ("chat-left", "chat-right"):
        result["default_layout"] = raw_layout
    if manifest.get("hide_brand") is True:
        result["hide_brand"] = True
    return JSONResponse(result)


@app.get("/api/plugin/{name}/theme.css")
async def plugin_theme_css(name: str) -> FileResponse:
    from app.tools.providers import LOADED_PLUGINS

    for plugin in LOADED_PLUGINS:
        if plugin["name"] == name:
            path = Path(plugin["path"]) / "theme.css"
            if path.exists():
                return FileResponse(
                    path, media_type="text/css",
                    headers={"Cache-Control": "public, max-age=3600"},
                )
            break
    # Fall back to the core default theme
    default = PROJECT_ROOT / "frontend" / "src" / "theme.css"
    if not default.exists():
        raise HTTPException(status_code=404, detail="Default theme not found")
    return FileResponse(
        default, media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/_html2canvas.js")
async def html2canvas_js() -> FileResponse:
    """Serve the html2canvas library into the Panel iframe.

    Loaded by ``panel_app.py`` via the same ``js_files`` mechanism used
    for ``_panel_shim.js`` — the shim then calls ``html2canvas(...)`` to
    rasterise the report on demand. The file lives in
    ``frontend/node_modules/`` (installed via ``npm install``); we serve
    it directly so we don't have to vendor a 200 KB blob in git.
    """
    path = (
        PROJECT_ROOT
        / "frontend" / "node_modules" / "html2canvas" / "dist" / "html2canvas.min.js"
    )
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "html2canvas not installed; run `npm install` in frontend/. "
                f"Expected {path}"
            ),
        )
    return FileResponse(
        path, media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/_panel_shim.js")
async def panel_shim_js():
    """Shim injected into the editable Panel iframe.

    Loaded via Panel's ``js_files`` mechanism (see ``panel_app.py``). Runs
    in the iframe before EditableTemplate's inline grid-setup script.

    Four responsibilities:

    1. **postMessage bridge** — parent (our ReportPane on the host site)
       posts ``{voittaAction: 'undo' | 'reset'}`` when the user clicks the
       toolbar buttons in our outer dark header; this script clicks the
       (hidden) Panel toolbar buttons by id.
    2. **Render-error pump** — capture ``window.error``,
       ``unhandledrejection``, and Bokeh's ``console.error("Error
       rendering Bokeh items")`` pattern, plus a ``ready`` signal once
       Bokeh's document is fully built. Each event is posted up to the
       parent ChatPane, which forwards it to
       ``/api/report-render-events`` so the awaiting ``show_holoviz_report``
       call (and the persisted log) get to see it.
    3. **Workaround for upstream Panel bug** — EditableTemplate's
       ``dragEnd`` / ``move`` handlers do ``undo_stack.pop().action``
       without guarding for ``undefined``. Muuri fires ``move`` outside
       drag flows (during initial sort, on subsequent edits etc.), so the
       stack is empty and a ``TypeError: reading 'action'`` floods the
       console. We intercept the ``window.muuriGrid`` assignment and wrap
       the emitter to swallow only this specific TypeError. Won't survive
       a Panel upgrade that fixes the bug — at which point the regex
       won't match and the wrapper simply re-throws like normal.
    4. **Edit-control polish** (only when ``editable=true``) — bump
       drag/delete/resize handle opacity so they're visible without
       hover, beef up the resize corner so it survives painting on top
       of a Bokeh canvas, and add a click-to-select outline ring on
       cards (``Esc`` to clear). Selection is purely visual; no event
       leaves the iframe.
    """

    from fastapi.responses import Response

    js = """(function(){
  // ---- 1. parent → iframe action bridge (undo/reset/screenshot) -----------
  window.addEventListener('message', function(e){
    // Errors forwarded from a NESTED srcdoc iframe (e.g. ctx.three_scene's
    // sandboxed viewer). e.source is the child iframe's window, not
    // window.parent — so the parent-only filter below would drop them.
    // Promote into the standard render-error stream so
    // get_report_render_errors picks them up.
    if (e.data && e.data.type === 'voitta_nested_error' && e.source !== window.parent) {
      _emitError({
        message: e.data.message,
        stack: e.data.stack,
        source: 'nested:' + (e.data.source || 'unknown'),
        url: e.data.url,
        line: e.data.line,
        col: e.data.col
      });
      return;
    }
    if (e.source !== window.parent) return;
    var a = e.data && e.data.voittaAction;
    if (a === 'screenshot') {
      // html2canvas is loaded as a sibling js_files entry; if it's not
      // ready yet we fail fast so the LLM gets a real error instead of
      // a silent hang. Capture the entire documentElement (scrollWidth
      // x scrollHeight) regardless of viewport size, then ship a webp
      // data URL back to the parent. Parent handles cropping.
      var rid = e.data.requestId;
      var scale = +e.data.scale || 1;
      var quality = +e.data.quality || 0.85;
      var format = e.data.format === 'png' ? 'image/png' : 'image/webp';
      function reply(payload) {
        try { window.parent.postMessage(Object.assign(
          {type: 'voitta_report_event', kind: 'screenshot_response', requestId: rid},
          payload
        ), '*'); } catch (e2) { /* parent gone */ }
      }
      if (typeof html2canvas !== 'function') {
        reply({ok: false, message: 'html2canvas not loaded'});
        return;
      }
      var docEl = document.documentElement;
      var fullW = Math.max(docEl.scrollWidth, docEl.clientWidth);
      var fullH = Math.max(docEl.scrollHeight, docEl.clientHeight);
      try {
        html2canvas(docEl, {
          allowTaint: true, useCORS: true, foreignObjectRendering: false,
          scale: scale, width: fullW, height: fullH,
          windowWidth: fullW, windowHeight: fullH,
          backgroundColor: '#ffffff',
          // Bokeh paints into 2D canvases by default — html2canvas can
          // pull those via toDataURL. WebGL canvases (rare) without
          // preserveDrawingBuffer come out blank; that's a known
          // upstream limit, not something we can paper over.
          logging: false,
        }).then(function(canvas){
          var dataUrl;
          try { dataUrl = canvas.toDataURL(format, quality); }
          catch (err) {
            // canvas tainted by a cross-origin image without CORS
            reply({ok: false, message: 'canvas_tainted: ' + (err && err.message)});
            return;
          }
          reply({
            ok: true, dataUrl: dataUrl,
            width: canvas.width, height: canvas.height,
            full_width: fullW, full_height: fullH,
            scale: scale, format: format,
          });
        }).catch(function(err){
          reply({ok: false, message: String((err && err.message) || err)});
        });
      } catch (err) {
        reply({ok: false, message: String((err && err.message) || err)});
      }
      return;
    }
    if (a === 'getEdits') {
      // Read current layout state out of the live Muuri grid + Bokeh
      // document, augmented with selection from our shim. The frontend
      // primitive ``get_report_edits`` is the only caller; it classifies
      // the response into no_active_report / active_no_edits / active.
      var rid = e.data.requestId;
      function replyEdits(payload) {
        try { window.parent.postMessage(Object.assign(
          {type: 'voitta_report_event', kind: 'edits_response', requestId: rid},
          payload
        ), '*'); } catch (e2) { /* parent gone */ }
      }
      try {
        var grid = window.muuriGrid;
        if (!grid) { replyEdits({ok: false, message: 'grid not ready'}); return; }
        var bk = window.Bokeh;
        var doc = (bk && bk.documents && bk.documents[0]) || null;
        var items = grid.getItems();
        var elements = items.map(function(item, idx){
          var el = item.getElement();
          var dataId = el.getAttribute('data-id');
          // Inline width is set by resize_item to ``calc( N% - 30px)``;
          // empty string means default (full row, 100%).
          var widthPct = 100;
          if (el.style.width) {
            var m = /\(\s*([\d.]+)%/.exec(el.style.width);
            if (m) widthPct = parseFloat(m[1]);
          }
          // Inline height is set by resize_item to ``Npx``; empty means
          // Bokeh-natural / stretch_both — represent as null so the LLM
          // can distinguish "user picked a height" from "default".
          var heightPx = null;
          if (el.style.height) {
            var hm = /^([\d.]+)px$/.exec(el.style.height);
            if (hm) heightPx = parseFloat(hm[1]);
          }
          // Resolve the Bokeh root by data-id — that's the model id the
          // template wrote into the DOM. ``model.name`` flows from the
          // Python ``name=`` argument, which is the identifier the LLM
          // can match against the script's source.
          var name = null, title = null;
          if (doc) {
            try {
              var model = doc.get_model_by_id(dataId);
              if (model) {
                name = model.name || null;
                if (model.title && typeof model.title.text === 'string') {
                  title = model.title.text || null;
                }
              }
            } catch (mErr) { /* model lookup is best-effort */ }
          }
          return {
            index: idx,
            id: dataId,
            name: name,
            title: title,
            width_pct: widthPct,
            height_px: heightPx,
            visible: item.isVisible(),
            selected: dataId === voittaSelectedId
          };
        });
        // Order-change detection vs the order we captured the first time
        // muuriGrid was assigned (see section 3 below). The LLM treats
        // any True here as "the user reordered cards" without having to
        // diff itself.
        var orderChanged = false;
        var initial = window.voittaInitialOrder;
        if (initial && initial.length === items.length) {
          for (var i = 0; i < items.length; i++) {
            if (items[i].getElement().getAttribute('data-id') !== initial[i]) {
              orderChanged = true;
              break;
            }
          }
        }
        replyEdits({
          ok: true,
          elements: elements,
          selected_id: voittaSelectedId,
          order_changed: orderChanged
        });
      } catch (err) {
        replyEdits({ok: false, message: String((err && err.message) || err)});
      }
      return;
    }
    var el = null;
    if (a === 'undo') el = document.getElementById('grid-undo');
    else if (a === 'reset') el = document.getElementById('grid-reset');
    if (el) el.click();
  });

  // voittaSelectedId / voittaInitialOrder are referenced by the getEdits
  // handler above. Initial declaration here means they exist even when
  // editable=false (handler returns ok:false in that case anyway).
  var voittaSelectedId = null;

  // ---- 2. iframe → parent render-error pump ------------------------------
  // Read render_id + report_id from the URL the parent built. If absent,
  // we still capture errors but they won't correlate with an awaiting
  // show_holoviz_report — they'll just land in the persisted log (if
  // report_id is known) or be dropped (if not).
  function _qs(name){
    try {
      var v = new URLSearchParams(window.location.search).get(name);
      return v == null ? '' : v;
    } catch (e) { return ''; }
  }
  var voittaRenderId = _qs('render_id');
  var voittaReportId = _qs('id');
  var voittaSentReady = false;
  var voittaErrorCount = 0;
  var VOITTA_MAX_ERRORS = 10;

  function _post(payload){
    try {
      window.parent.postMessage(
        Object.assign({type: 'voitta_report_event'}, payload), '*'
      );
    } catch (e) { /* parent gone — give up */ }
  }
  function _emitError(payload){
    if (!voittaRenderId && !voittaReportId) return;
    if (++voittaErrorCount > VOITTA_MAX_ERRORS) return;
    _post({
      kind: 'error',
      render_id: voittaRenderId,
      report_id: voittaReportId,
      message: String(payload.message || '').slice(0, 4000),
      stack: payload.stack ? String(payload.stack).slice(0, 6000) : null,
      source: payload.source || 'unknown',
      url: payload.url || null,
      line: payload.line || null,
      col: payload.col || null
    });
  }
  function _emitReady(){
    if (voittaSentReady) return;
    voittaSentReady = true;
    if (!voittaRenderId && !voittaReportId) return;
    _post({
      kind: 'ready',
      render_id: voittaRenderId,
      report_id: voittaReportId,
      source: 'ready'
    });
  }

  window.addEventListener('error', function(e){
    var err = e && e.error;
    _emitError({
      message: (err && err.message) || e.message || String(e),
      stack: err && err.stack,
      source: 'window.error',
      url: e.filename, line: e.lineno, col: e.colno
    });
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    var r = e && e.reason;
    _emitError({
      message: (r && (r.message || String(r))) || 'unhandled rejection',
      stack: r && r.stack,
      source: 'unhandledrejection'
    });
  });
  // Hook console.error so we catch Bokeh's swallowed "Error rendering
  // Bokeh items" — it logs the inner error there but does not re-throw,
  // so window.error never sees it. Bokeh typically calls
  // ``console.error("Error rendering Bokeh items:", innerErr)`` — we
  // concat both the label AND the inner err.message so the LLM sees
  // the actionable string ("SlickGrid Cannot find stylesheet") even
  // though it's in arg[1].
  var _origConsoleError = console.error.bind(console);
  function _stringifyArg(a){
    if (a == null) return '';
    if (typeof a === 'string') return a;
    if (a.message) {
      // Error / DOMException / Bokeh error.
      var name = a.name || (a.constructor && a.constructor.name) || 'Error';
      return name + ': ' + a.message;
    }
    try { return String(a); } catch (e) { return '[unstringifiable]'; }
  }
  console.error = function(){
    try {
      var parts = [];
      var stack = null;
      for (var i = 0; i < arguments.length; i++) {
        var a = arguments[i];
        parts.push(_stringifyArg(a));
        if (!stack && a && a.stack) stack = a.stack;
      }
      var msg = parts.join(' ');
      // Be permissive — better one false positive than missing a real one.
      // Match anything Bokeh-shaped, anything table/SlickGrid-shaped, or
      // any plain "Error rendering …" Panel emits.
      if (/error rendering|slickgrid|bokeh|tabulator|panel\.error/i.test(msg)) {
        _emitError({message: msg.slice(0, 4000), stack: stack, source: 'bokeh'});
      }
    } catch (e) { /* don't break console.error */ }
    return _origConsoleError.apply(null, arguments);
  };

  // Detect "document ready" by polling for Bokeh.documents being non-empty
  // and all roots layout-stable. Simpler than wiring into Bokeh's events
  // (the API surface differs across versions). Fires at most once.
  var _readyTries = 0;
  var _readyTimer = setInterval(function(){
    _readyTries++;
    var bk = window.Bokeh;
    if (bk && bk.documents && bk.documents.length > 0) {
      var doc = bk.documents[0];
      // Heuristic: at least one root has been added AND the embedder
      // rendered the corresponding view. We don't have a robust API,
      // so consider "1+ root" ready after a short stabilisation delay.
      var roots = (doc.roots && doc.roots()) || [];
      if (roots.length > 0) {
        clearInterval(_readyTimer);
        setTimeout(_emitReady, 250);
        return;
      }
    }
    if (_readyTries > 80) { // 80 * 100ms = 8s; long-tail fallback
      clearInterval(_readyTimer);
      _emitReady(); // tell parent we never saw a doc — close the await
    }
  }, 100);

  // ---- 3. EditableTemplate / Muuri empty-stack bug suppression -----------
  // Intercept window.muuriGrid assignment to wrap the emitter before any
  // drag events fire. The buggy handlers throw inside grid._emitter.emit;
  // try/catching there is enough to neutralise the noise.
  var actualGrid;
  Object.defineProperty(window, 'muuriGrid', {
    configurable: true,
    get: function(){ return actualGrid; },
    set: function(g){
      actualGrid = g;
      // Snapshot initial card order so getEdits can flag user-reorder.
      // Captured once, on first assignment — subsequent reassignments
      // (rare; only on full template re-render) keep the original.
      if (g && !window.voittaInitialOrder) {
        try {
          window.voittaInitialOrder = g.getItems().map(function(it){
            return it.getElement().getAttribute('data-id');
          });
        } catch (oErr) { /* best-effort */ }
      }
      if (g && g._emitter && !g._emitter._voittaPatched) {
        var origEmit = g._emitter.emit;
        g._emitter.emit = function(){
          try {
            return origEmit.apply(this, arguments);
          } catch (err) {
            if (err instanceof TypeError &&
                /reading [\"']?action[\"']?/.test(String(err && err.message))) {
              return; // known Panel EditableTemplate empty-undo-stack bug
            }
            throw err;
          }
        };
        g._emitter._voittaPatched = true;
      }
    }
  });

  // ---- 4. Visual edit controls: handle visibility + element selection ----
  // Only meaningful when ?editable=true. Three things:
  //   a) Bump default handle opacity (0.2 → 0.6) so drag/delete/resize are
  //      discoverable without hovering.
  //   b) Beef up the resize handle (size + opaque background) so it stays
  //      legible when a Bokeh canvas paints under it.
  //   c) Click-to-select: card click adds .voitta-selected (outline ring),
  //      click outside any card or Esc clears it. Selection is purely
  //      visual — no postMessage out, no preventDefault, so Bokeh tools
  //      keep working.
  if (_qs('editable') === 'true') {
    var voittaStyle = document.createElement('style');
    voittaStyle.textContent = `
      /* Disable per-card scroll. Panel's scroll() in editable.html flips
         overflow-y:auto when content > card; that turns the card into a
         scroll container, which makes absolutely-positioned handles
         anchor to scrollHeight (the bottom of the *content*) instead of
         the visible card edge. Result: handles drift mid-card as you
         scroll. With our server-side stretch_both promotion, content
         already adapts to card size, so scroll is rarely needed — and
         when content really doesn't fit, the user can resize the card. */
      .muuri-grid-item { overflow: hidden !important; }
      .muuri-handle { opacity: 0.6 !important; }
      .muuri-handle:hover, .muuri-handle:focus { opacity: 1 !important; }
      .muuri-handle.resize {
        width: 22px !important; height: 22px !important;
        background-size: 18px !important;
        background-color: rgba(255,255,255,0.78);
        border-radius: 4px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.18);
      }
      .muuri-handle.resize:hover { cursor: nwse-resize; }
      .muuri-grid-item.voitta-selected {
        outline: 2px solid #3b82f6;
        outline-offset: -2px;
      }
    `;
    (document.head || document.documentElement).appendChild(voittaStyle);

    // voittaSelectedId is hoisted to outer scope (declared up by the
    // postMessage listener) so getEdits can read it.
    function voittaSetSelected(id){
      if (voittaSelectedId === id) return;
      if (voittaSelectedId) {
        var prev = document.querySelector(
          '.muuri-grid-item[data-id="' + CSS.escape(voittaSelectedId) + '"]'
        );
        if (prev) prev.classList.remove('voitta-selected');
      }
      voittaSelectedId = id;
      if (id) {
        var next = document.querySelector(
          '.muuri-grid-item[data-id="' + CSS.escape(id) + '"]'
        );
        if (next) next.classList.add('voitta-selected');
      }
    }

    // Capture phase so we see clicks even if Bokeh stops propagation.
    // Never preventDefault — plot interactivity must keep working.
    document.addEventListener('click', function(e){
      var node = e.target;
      while (node && node.nodeType === 1) {
        if (node.classList && node.classList.contains('muuri-grid-item')) {
          var id = node.getAttribute('data-id');
          if (id) voittaSetSelected(id);
          return;
        }
        node = node.parentNode;
      }
      voittaSetSelected(null);
    }, true);

    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape') voittaSetSelected(null);
    });
  }
})();"""
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/settings")
async def get_user_settings() -> dict:
    """Return the persisted user settings blob (LLM keys, provider, model,
    caps). Empty object if the user hasn't saved anything yet.

    Bound to localhost-only via uvicorn host=127.0.0.1; no auth beyond
    that. The blob is intentionally opaque to the server — see
    app/services/user_settings.py.
    """
    from app import config as _cfg
    from app.services import user_settings

    try:
        blob = user_settings.read()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"settings file is corrupt: {exc}",
        )
    # Inject server-side defaults for fields the blob doesn't yet have.
    # The frontend's coerceSettings re-validates on read; we only provide
    # the value so it beats DEFAULT_SETTINGS ("chat-right") when the
    # operator has overridden VOITTA_DEFAULT_LAYOUT.
    if "layout" not in blob:
        blob = {**blob, "layout": _cfg.DEFAULT_LAYOUT}
    return blob


@app.put("/api/settings")
async def put_user_settings(request: Request) -> dict:
    """Merge a partial settings update into the persisted blob.

    Top-level keys present in the request body OVERWRITE the
    corresponding stored keys; keys absent from the request are
    PRESERVED. This matters because backend-only state lives in the
    same file (e.g. ``googleOAuth.clientId`` / ``…clientSecret`` /
    ``…tokens``); a wholesale replacement from the frontend would
    blow them away every time the user clicks Save.

    Nested merge is intentionally NOT done — keep the contract
    simple. The frontend is expected to send a flat payload of only
    the fields it owns (provider, *ApiKey, *Model, maxTokens,
    maxToolIterations).
    """
    from app.services import user_settings

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    existing = user_settings.read()
    existing.update(body)
    user_settings.write(existing)
    return {"ok": True}


@app.get("/api/google/status")
async def google_oauth_status() -> dict:
    """Connection status for the Settings UI."""
    from app.services import google_oauth

    return google_oauth.status()


@app.get("/api/google/oauth/start")
async def google_oauth_start():
    """Begin the OAuth flow — redirect the browser to Google's consent
    screen. The widget opens this URL in a popup; on completion the
    callback below closes the popup."""
    from fastapi.responses import RedirectResponse, HTMLResponse
    from app.services import google_oauth

    if not google_oauth.is_configured():
        return HTMLResponse(
            "<h2>Google OAuth not configured</h2>"
            "<p>Add <code>googleOAuth.clientId</code> and "
            "<code>googleOAuth.clientSecret</code> to "
            "<code>~/.config/voitta-bookmarklet/settings.json</code>, "
            "then retry.</p>",
            status_code=400,
        )
    url, _state = google_oauth.build_authorize_url()
    return RedirectResponse(url, status_code=302)


@app.get("/api/google/oauth/callback")
async def google_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Receive the authorization code from Google, exchange it for
    tokens, persist to settings.json, and self-close the popup so
    the user lands back in the chat pane."""
    from fastapi.responses import HTMLResponse
    from app.services import google_oauth

    def _close_html(title: str, body: str, ok: bool) -> HTMLResponse:
        # Tiny self-closing page. The chat pane is polling
        # /api/google/status after it opened the popup, so it picks
        # up the new state on its own — no need to postMessage back.
        color = "#0a8a3a" if ok else "#b00020"
        return HTMLResponse(
            f"""<!doctype html><html><head><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif;
         color: #222; background: #fafafa;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; }}
  .card {{ background: white; padding: 28px 32px; border-radius: 8px;
         box-shadow: 0 1px 3px rgba(0,0,0,0.08); max-width: 420px;
         text-align: center; }}
  h2 {{ margin: 0 0 8px; font-size: 18px; color: {color}; }}
</style></head>
<body><div class="card"><h2>{title}</h2><p>{body}</p>
<p style="color:#888;font-size:12px;">You can close this window.</p>
</div>
<script>setTimeout(function(){{ try {{ window.close(); }} catch(e){{}} }}, 1500);</script>
</body></html>"""
        )

    if error:
        return _close_html("Connection cancelled", f"Google returned: <code>{error}</code>.", ok=False)
    if not code or not state:
        return _close_html("Bad callback", "Missing code/state.", ok=False)
    if not google_oauth.consume_state(state):
        return _close_html(
            "Invalid state",
            "The state token didn't match a pending OAuth request.",
            ok=False,
        )
    try:
        tok = await google_oauth.exchange_code(code)
    except Exception as exc:
        return _close_html("Token exchange failed", str(exc)[:300], ok=False)

    email = tok.get("account_email") or "(unknown)"
    return _close_html(
        "Google Drive connected",
        f"Signed in as <b>{email}</b>. The Drive tools are now available to the LLM.",
        ok=True,
    )


@app.get("/api/google/config")
async def google_oauth_get_config() -> dict:
    """Return the saved OAuth client_id/client_secret for the Settings
    UI to prefill its Configure form. Localhost-only, same trust
    boundary as GET /api/settings."""
    from app.services import google_oauth

    return google_oauth.get_client_config()


@app.post("/api/google/configure")
async def google_oauth_set_config(request: Request) -> dict:
    """Persist a new clientId/clientSecret pair. If the user was
    connected, the existing tokens get revoked + cleared (they belong
    to the old client). Body: ``{"clientId": "...", "clientSecret": "..."}``."""
    from app.services import google_oauth

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    cid = body.get("clientId")
    csec = body.get("clientSecret")
    try:
        await google_oauth.set_client_credentials(cid or "", csec or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **google_oauth.status()}


@app.post("/api/drive-pickup/cancel")
async def drive_pickup_cancel(request: Request) -> dict:
    """User clicked × on the Drive download modal — flip the cancel flag.

    The pickup tool's watcher polls ``drive_pickup.is_cancelled(modal_id)``
    on every tick and returns immediately when this is set. Body shape:
    ``{"modal_id": "<16-hex>"}``. Idempotent — repeated POSTs for the
    same id return ``{ok: true, was_pending: false}``.
    """
    from app.services import drive_pickup

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    modal_id = (body or {}).get("modal_id")
    if not isinstance(modal_id, str) or not modal_id:
        raise HTTPException(status_code=400, detail="modal_id required")
    flipped = drive_pickup.set_cancelled(modal_id)
    return {"ok": True, "modal_id": modal_id, "was_pending": flipped}


# ---- auth routes ----------------------------------------------------------


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict:
    """Tell the bookmarklet whether it needs to show the login screen.

    ``localhost_mode`` is True when the backend is running in
    "skip auth" mode — frontend should mount the chat directly.
    Otherwise ``authenticated`` reflects the cookie state.
    """
    from app import config as _cfg

    cookie = request.cookies.get(_cfg.AUTH_COOKIE_NAME)
    return {
        "localhost_mode": _cfg.LOCALHOST_MODE,
        "authenticated": (cookie is not None and cookie == _cfg.API_KEY),
    }


@app.post("/api/auth/login")
async def auth_login(request: Request) -> JSONResponse:
    """Validate an API key and set the session cookie.

    Body: ``{"api_key": "..."}``. Wrong key → 401. Right key → 200 with
    ``Set-Cookie`` header. Cookie is HttpOnly + SameSite=None so it
    rides on cross-origin fetches and EventSource and iframe loads
    from any host page the bookmarklet runs on.
    """
    from app import config as _cfg

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    submitted = (body.get("api_key") or "").strip()
    if not submitted:
        raise HTTPException(status_code=400, detail="api_key required")

    if submitted != _cfg.API_KEY:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "invalid_api_key"},
        )

    response = JSONResponse({"ok": True, "authenticated": True})
    # SameSite=None requires Secure (TLS). The .app and run.sh both
    # serve HTTPS by default. ``--http`` mode is loopback-only so
    # SameSite=Lax is fine there; we fall back when the request is
    # http://. ``HttpOnly`` blocks document.cookie reads — the
    # widget instead checks /api/auth/status for the auth bit.
    is_https = request.url.scheme == "https"
    response.set_cookie(
        _cfg.AUTH_COOKIE_NAME,
        _cfg.API_KEY,
        httponly=True,
        secure=is_https,
        samesite="none" if is_https else "lax",
        max_age=60 * 60 * 24 * 30,  # 30 days
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout() -> JSONResponse:
    """Clear the session cookie. Idempotent; always returns 200."""
    from app import config as _cfg

    response = JSONResponse({"ok": True})
    response.delete_cookie(_cfg.AUTH_COOKIE_NAME, path="/")
    return response


@app.post("/api/google/disconnect")
async def google_oauth_disconnect() -> dict:
    """Revoke + clear stored tokens. Drive tools become hidden from
    the LLM again immediately."""
    from app.services import google_oauth

    await google_oauth.disconnect()
    return {"ok": True, "connected": False}


@app.post("/api/report-render-events")
async def report_render_events(request: Request) -> dict:
    """Receive a render-lifecycle event from a report iframe.

    The shim in ``/api/_panel_shim.js`` postMessages ready/error events
    up to the parent ChatPane, which forwards them here. ``record()``
    persists to the per-script render log and signals any
    ``show_holoviz_report`` await on this ``render_id``.

    Body shape (all strings except ``line``/``col``)::

        {
          "render_id": "...",        # required
          "report_id": "...",        # required (slug)
          "kind": "ready" | "error", # required
          "message": "...",          # error only
          "stack": "...",            # error only
          "source": "window.error" | "unhandledrejection" |
                    "console.error" | "bokeh" | "ready",
          "url": "...",              # script url where the error fired
          "line": 123,
          "col": 45
        }
    """
    from app.services import render_events

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    render_id = str(body.get("render_id") or "").strip()
    report_id = str(body.get("report_id") or "").strip()
    kind = str(body.get("kind") or "").strip()
    if not render_id or not report_id or kind not in ("ready", "error"):
        raise HTTPException(
            status_code=400,
            detail="render_id, report_id, and kind in {ready, error} required",
        )

    def _maybe_int(v: object) -> int | None:
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    ev = render_events.record(
        render_id=render_id,
        report_id=report_id,
        kind=kind,  # type: ignore[arg-type]
        message=body.get("message") if isinstance(body.get("message"), str) else None,
        stack=body.get("stack") if isinstance(body.get("stack"), str) else None,
        source=body.get("source") if isinstance(body.get("source"), str) else None,
        url=body.get("url") if isinstance(body.get("url"), str) else None,
        line=_maybe_int(body.get("line")),
        col=_maybe_int(body.get("col")),
    )
    return {"ok": True, "ts": ev.ts}


@app.get("/widget.js")
async def widget_js() -> FileResponse:
    bundle = FRONTEND_DIST / "widget.js"
    if not bundle.exists():
        return JSONResponse(
            status_code=503,
            content={
                "error": "frontend bundle missing",
                "hint": "build the frontend into frontend/dist/widget.js (see docs/02-frontend.md)",
            },
        )
    return FileResponse(
        bundle,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


# ── Panel-served reports ────────────────────────────────────────────────────
# Reports run as live Panel apps mounted at /panel/reports. Each browser
# session opens a websocket back to this process; drag/resize on
# EditableTemplate commits via the Bokeh comm round-trip (which static
# .save() can't do). See app/services/panel_app.py for the factory.
from app.services.panel_app import panel_factory  # noqa: E402
from panel.io.fastapi import add_applications  # noqa: E402

add_applications({"/panel/reports": panel_factory}, app=app)

# Workaround for a bokeh_fastapi bug: WSHandler.send_message catches
# WebSocketDisconnect but not the RuntimeError uvicorn raises when the
# socket closes mid-push (e.g. iframe re-shown with a cache-bust URL).
# See app/services/_bokeh_ws_patch.py.
from app.services._bokeh_ws_patch import patch_send_message  # noqa: E402

patch_send_message()


@app.get("/api/artifacts")
async def list_artifacts() -> dict:
    """Return a tree of server-side artifacts for the in-pane file browser.

    Walks the four buckets under ``python_storage/``:

      * ``cache/`` — snapshot dirs from downloads + their files
        (plus ``script_output/<run_id>/`` image runs).
      * ``compute/`` — LLM-authored compute scripts (code.py +
        meta.json) and their ``runs/<run_id>/`` outputs.
      * ``reports/`` — HoloViz report scripts.
      * ``flows/`` — flow report scripts.

    Paths in the response are relative to PROJECT_ROOT so the browser
    never sees absolute filesystem paths.
    """

    def _enrich_snapshot_dir(path: Path) -> dict | None:
        """If this is a python_storage snapshot dir, return its
        ``meta.json``-derived display fields. Otherwise None."""
        if not path.name.startswith("snapshot_"):
            return None
        meta_path = path / "meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            return None
        if not isinstance(meta, dict):
            return None
        origin = meta.get("origin") or {}
        # Best-known display name: stored_name (drive_file) →
        # original_name → first file → handle.
        display_name = (
            meta.get("stored_name")
            or meta.get("original_name")
        )
        if not display_name and isinstance(meta.get("files"), list) and meta["files"]:
            display_name = meta["files"][0].get("name")
        return {
            "handle": meta.get("handle"),
            "kind": meta.get("kind"),
            "display_name": display_name,
            "origin": {
                "source": origin.get("source"),
                "account": origin.get("account"),
                "path": origin.get("path"),
                "url": origin.get("url"),
            } if origin else None,
        }

    def _node(path, root):
        rel = str(path.relative_to(root))
        try:
            st = path.stat()
            mtime = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
        except OSError:
            mtime = None
        if path.is_dir():
            children = []
            try:
                entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                entries = []
            for child in entries:
                if child.name.startswith("."):
                    continue
                children.append(_node(child, root))
            size = sum(c.get("size") or 0 for c in children)
            node: dict = {
                "name": path.name,
                "path": rel,
                "kind": "dir",
                "size": size,
                "mtime": mtime,
                "child_count": len(children),
                "children": children,
            }
            snap = _enrich_snapshot_dir(path)
            if snap:
                node["snapshot"] = snap
            return node
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {
            "name": path.name,
            "path": rel,
            "kind": "file",
            "size": size,
            "mtime": mtime,
        }

    roots: list[dict] = []
    for sub in (
        "python_storage/cache",
        "python_storage/compute",
        "python_storage/reports",
        "python_storage/flows",
    ):
        p = PROJECT_ROOT / sub
        if p.exists() and p.is_dir():
            roots.append(_node(p, PROJECT_ROOT))
        else:
            roots.append(
                {"name": sub.split("/")[-1], "path": sub, "kind": "dir",
                 "size": 0, "mtime": None,
                 "child_count": 0, "children": [], "missing": True}
            )

    return {"roots": roots, "total_size": sum(r.get("size") or 0 for r in roots)}


# ---------------------------------------------------------------------------
# Artifact mutation — delete / rename / run.
#
# The in-pane Finder-style browser (ArtifactsView) writes through these
# endpoints. Each one enforces a strict allow-list: only the four "unit"
# shapes are mutable, and never the namespace roots above them. Mutating
# a derived file (a meta.json, a single run output image) is impossible
# by design — the unit is the snapshot dir, the slug dir, or the run
# dir. This keeps the LLM's data model coherent with what the user can
# do by hand: the same things `delete_python_storage(handle)` cleans up.
# ---------------------------------------------------------------------------


_ARTIFACT_SNAPSHOT_RE = re.compile(r"^python_storage/cache/snapshot_[A-Za-z0-9_-]+$")
_ARTIFACT_SLUG_RE = re.compile(r"^python_storage/(compute|reports|flows)/[a-z0-9_-]{1,64}$")
_ARTIFACT_RUN_RE = re.compile(
    r"^python_storage/compute/[a-z0-9_-]{1,64}/runs/[A-Za-z0-9_-]{4,64}$"
)
_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _classify_artifact_path(rel: str) -> str | None:
    """Return the unit kind for ``rel`` or ``None`` if it's not mutable.

    Kinds:
      * ``"snapshot"`` — a python_storage snapshot dir (handle is canonical;
        rename adjusts ``meta.json::display_name``).
      * ``"compute"`` / ``"reports"`` / ``"flows"`` — a script slug dir
        (the dir name IS the slug; rename = real dir rename).
      * ``"run"`` — a compute run dir under ``runs/<run_id>`` (canonical;
        not renameable).

    The check is path-shape only; existence is verified by the caller
    after the regex matches.
    """
    if _ARTIFACT_SNAPSHOT_RE.match(rel):
        return "snapshot"
    if _ARTIFACT_RUN_RE.match(rel):
        return "run"
    m = _ARTIFACT_SLUG_RE.match(rel)
    if m:
        return m.group(1)  # "compute" | "reports" | "flows"
    return None


def _resolve_artifact_path(rel: str) -> tuple[Path, str]:
    """Validate ``rel`` against the allow-list and resolve to an absolute
    path under PROJECT_ROOT. Raises HTTPException on any rule violation.

    Returns ``(absolute_path, unit_kind)``. The absolute path is guaranteed
    to live under PROJECT_ROOT (no traversal escape).
    """
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    kind = _classify_artifact_path(rel)
    if kind is None:
        raise HTTPException(
            status_code=400, detail="not_a_unit (path is a namespace or derived item)"
        )
    abs_path = (PROJECT_ROOT / rel).resolve()
    root = PROJECT_ROOT.resolve()
    if not (abs_path == root or str(abs_path).startswith(str(root) + "/")):
        raise HTTPException(status_code=400, detail="path escapes project root")
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return abs_path, kind


@app.delete("/api/artifacts/{rel_path:path}")
async def delete_artifact(rel_path: str) -> dict:
    """Remove one artifact unit. Allow-list enforced.

    Snapshots route through ``python_storage.delete(handle)`` so handle-
    aware bookkeeping fires (mirrors the LLM-callable
    ``delete_python_storage`` tool). Slug + run units use ``shutil.rmtree``
    after the allow-list check — there's no handle bookkeeping for those.
    """
    import shutil

    abs_path, kind = _resolve_artifact_path(rel_path)

    if kind == "snapshot":
        # ``rel_path = python_storage/cache/snapshot_<handle>`` — recover the
        # handle from meta.json (canonical) rather than parsing the dir
        # name, in case anything ever drifts.
        from app.services import python_storage

        meta_path = abs_path / "meta.json"
        handle: str | None = None
        if meta_path.exists():
            try:
                handle = json.loads(meta_path.read_text()).get("handle")
            except Exception:
                handle = None
        if not handle:
            # Fall back to dir-name suffix; still validate against the
            # canonical regex shape so a hand-mangled dir can't sneak in
            # an unsafe handle string.
            handle = abs_path.name.removeprefix("snapshot_")
        if not python_storage.delete(handle):
            # delete() returns False when the handle wasn't found, but
            # the dir clearly exists — rmtree as fallback so the user
            # isn't stuck staring at a zombie row.
            shutil.rmtree(abs_path, ignore_errors=False)
        return {"ok": True, "deleted": rel_path, "kind": kind}

    # Plain script-tree directory: rmtree.
    shutil.rmtree(abs_path, ignore_errors=False)
    return {"ok": True, "deleted": rel_path, "kind": kind}


@app.patch("/api/artifacts/{rel_path:path}")
async def patch_artifact(rel_path: str, body: dict) -> dict:
    """Rename one artifact unit. Allow-list enforced.

    Body shape depends on the unit:

      * snapshot: ``{"display_name": "..."}`` — edits ``meta.json``,
        does NOT rename the dir (the handle is canonical and used by
        the LLM and python_storage internals).
      * compute / reports / flows slug: ``{"slug": "new-slug"}`` —
        renames the dir on disk. New slug must match ``[a-z0-9_-]{1,64}``
        and must not collide with an existing sibling.
      * run: never renameable (canonical id).
    """
    abs_path, kind = _resolve_artifact_path(rel_path)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    if kind == "snapshot":
        new_name = body.get("display_name")
        if not isinstance(new_name, str):
            raise HTTPException(
                status_code=400, detail="display_name (string) required for snapshots"
            )
        new_name = new_name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="display_name cannot be empty")
        meta_path = abs_path / "meta.json"
        if not meta_path.exists():
            raise HTTPException(status_code=404, detail="meta.json missing for snapshot")
        try:
            meta = json.loads(meta_path.read_text())
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"meta.json unreadable: {exc}")
        if not isinstance(meta, dict):
            raise HTTPException(status_code=500, detail="meta.json malformed")
        # ``display_name`` is computed from ``stored_name`` /
        # ``original_name`` / ``files[0].name`` (see _enrich_snapshot_dir
        # in /api/artifacts). Writing ``stored_name`` is the most
        # specific override and what the browser then surfaces.
        meta["stored_name"] = new_name
        meta_path.write_text(json.dumps(meta, indent=2))
        return {"ok": True, "path": rel_path, "display_name": new_name, "kind": kind}

    if kind in ("compute", "reports", "flows"):
        new_slug = body.get("slug")
        if not isinstance(new_slug, str) or not _SLUG_RE.match(new_slug):
            raise HTTPException(
                status_code=400, detail="slug must match [a-z0-9_-]{1,64}"
            )
        new_path = abs_path.parent / new_slug
        if new_path.exists():
            raise HTTPException(status_code=409, detail=f"slug {new_slug!r} already exists")
        abs_path.rename(new_path)
        new_rel = f"python_storage/{kind}/{new_slug}"
        return {"ok": True, "path": new_rel, "kind": kind}

    raise HTTPException(status_code=400, detail=f"unit {kind!r} is not renameable")


@app.post("/api/artifacts/{rel_path:path}/run")
async def run_artifact(rel_path: str) -> dict:
    """Re-execute a report slug and return what the frontend needs to
    mount the iframe / flow pane.

    Two flavours, distinguished by the slug's parent dir:

      * ``python_storage/reports/<slug>`` — HoloViz Panel iframe. We
        mint a ``render_id``, register an awaiter so render-time
        errors are captured the same way ``show_holoviz_report`` does,
        and return the iframe URL the frontend should hand to
        ``show_report``.
      * ``python_storage/flows/<slug>`` — server-side build of the
        flow definition (same path ``show_flow_report`` uses),
        returned in the response body. The frontend hands it to
        ``show_flow_report``.

    Compute slugs and run dirs are not runnable: those are LLM-only
    flows that need a ToolCtx.
    """
    abs_path, kind = _resolve_artifact_path(rel_path)
    if kind not in ("reports", "flows"):
        raise HTTPException(
            status_code=400,
            detail=f"unit {kind!r} is not runnable (only reports/ and flows/)",
        )
    code_path = abs_path / "code.py"
    if not code_path.exists():
        raise HTTPException(status_code=404, detail="code.py missing")
    slug = abs_path.name
    title = f"Report {slug}" if kind == "reports" else f"Flow {slug}"

    from app.services import render_events

    render_id = render_events.new_render_id()
    render_events.begin_await(render_id, slug)

    if kind == "reports":
        from urllib.parse import quote

        cache_t = int(time.time() * 1000)
        path = (
            f"/panel/reports?id={quote(slug, safe='')}"
            f"&render_id={quote(render_id, safe='')}"
            f"&_t={cache_t}"
        )
        return {
            "ok": True,
            "kind": "holoviz",
            "report_id": slug,
            "render_id": render_id,
            "title": title,
            "path": path,
        }

    # kind == "flows"
    from app.services import flows
    from app.services.scripts import ScriptError

    try:
        definition, _log_lines = flows.build_flow_definition(slug)
    except ScriptError as exc:
        render_events.end_await(render_id)
        raise HTTPException(status_code=400, detail=f"build error: {exc}")
    return {
        "ok": True,
        "kind": "flow",
        "report_id": slug,
        "render_id": render_id,
        "title": title,
        "definition": definition,
    }


@app.get("/api/script-output/{slug}/{run_id}/{filename}")
async def get_script_output(slug: str, run_id: str, filename: str) -> FileResponse:
    """Serve files written by ``ctx.image()`` from a compute script run.
    Files live under ``python_storage/compute/<slug>/runs/<run_id>/<file>``;
    the slug+run_id+filename are tightly validated and the resolved
    path must stay inside the compute root."""

    if not re.match(r"^[a-z0-9_-]{1,64}$", slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    if not re.match(r"^[a-f0-9]{6,32}$", run_id):
        raise HTTPException(status_code=400, detail="invalid run_id")
    if not re.match(r"^[a-zA-Z0-9._-]{1,64}$", filename):
        raise HTTPException(status_code=400, detail="invalid filename")

    from app.services.scripts import SCRIPTS_COMPUTE

    candidate = (SCRIPTS_COMPUTE / slug / "runs" / run_id / filename).resolve()
    root = SCRIPTS_COMPUTE.resolve()
    if not str(candidate).startswith(str(root) + "/"):
        raise HTTPException(status_code=400, detail="path escapes compute root")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")

    suffix = candidate.suffix.lower()
    media = (
        "image/png"
        if suffix == ".png"
        else "image/jpeg"
        if suffix in (".jpg", ".jpeg")
        else "image/svg+xml"
        if suffix == ".svg"
        else "application/octet-stream"
    )
    return FileResponse(candidate, media_type=media)


@app.get("/api/python-storage/{handle}/{filename}")
async def get_python_storage_file(handle: str, filename: str) -> FileResponse:
    """Serve a single file out of a ``python_storage`` snapshot directory.

    Used by tools that produce browser-displayable artefacts (e.g.
    ``screenshot_report``) and want the chat pane to render them via a
    plain ``<img src=…>`` rather than a ``data:`` URL — host pages
    typically have a CSP that blocks ``data:`` in ``img-src``, so
    backend HTTPS URLs are the only path that consistently renders.

    Path: ``python_storage/cache/snapshot_<handle>/<filename>``. Both
    segments are tightly validated and the resolved path must stay
    inside the cache root.
    """
    # Handle shape per python_storage._new_handle: ``py_<8 hex>``. Allow
    # a small range around that to stay forward-compatible if the
    # handle generator changes.
    if not re.match(r"^[A-Za-z0-9_-]{3,64}$", handle):
        raise HTTPException(status_code=400, detail="invalid handle")
    if not re.match(r"^[A-Za-z0-9._-]{1,200}$", filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    if filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid filename")

    from app.services.python_storage import STORAGE_ROOT

    candidate = (STORAGE_ROOT / f"snapshot_{handle}" / filename).resolve()
    root = STORAGE_ROOT.resolve()
    if not str(candidate).startswith(str(root) + "/"):
        raise HTTPException(status_code=400, detail="path escapes python_storage root")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")

    suffix = candidate.suffix.lower()
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".csv": "text/csv",
        ".txt": "text/plain",
    }.get(suffix, "application/octet-stream")
    return FileResponse(candidate, media_type=media)


if FRONTEND_DIST.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIST), name="static")
