"""Hardened-site bridge — popup relay + page-side network shims.

Why this exists
---------------
On ordinary pages the bookmarklet injects ``<script src=…/widget.js>`` and
the widget talks to the backend directly over https. On pages with a strict
Content-Security-Policy (Salesforce Lightning is the motivating case) two
directives kill that:

  * ``script-src`` forbids loading a ``<script>`` from 127.0.0.1, and
  * ``connect-src`` forbids the page opening *any* connection (fetch / XHR /
    WebSocket) to 127.0.0.1 — so even an eval'd widget can't reach the backend.

CSP, however, is per-document and governs *network* egress. It does NOT
govern ``window.open`` (a top-level navigation, not ``frame-src``) nor
``window.postMessage`` (in-process messaging, not ``connect-src``). So the
widget is bridged through a popup window served from the backend's own
plain-http origin (``PLAINTEXT_PORT``):

    Salesforce tab (CSP-locked)            Popup = http://127.0.0.1:PLAINTEXT (no CSP)
    ─────────────────────────────         ─────────────────────────────────────────
    bookmarklet → window.open(popup)
    widget (eval'd) runs here              relay.js runs here
      fetch/WebSocket SHIMMED ──postMessage──▶ real fetch / WebSocket (same-origin → free)
                              ◀──postMessage──   results / frames

The popup is the only context that actually touches the backend; the locked
page only ever calls ``window.open`` and ``postMessage``, both CSP-exempt.

Pieces
------
* ``GET /bridge``        → ``_BRIDGE_HTML``  (the popup document; loads relay)
* ``GET /bridge/relay.js`` → ``_RELAY_JS``  (runs in the popup)
* ``GET /bridge/boot.js``  → ``_BOOT_JS``   (runs in the locked page; installs
                                              the fetch/WebSocket shims, then
                                              loads the widget)

The locked page never fetches ``boot.js`` itself (``connect-src`` blocks it);
the popup fetches it same-origin and hands the source to the opener over
``postMessage`` for the page to ``eval`` (``'unsafe-eval'`` is permitted, and
this CSP sets no ``require-trusted-types-for``).

This module is plain-http only in practice but is mounted on the shared ASGI
app, so the routes exist on both listeners.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

_JS_HEADERS = {"Cache-Control": "no-store"}


# ───────────────────────────────────────────────────────────────────────────
# Popup document
# ───────────────────────────────────────────────────────────────────────────
_BRIDGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Voitta bridge</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html,body{margin:0;height:100%;font:13px/1.5 -apple-system,system-ui,sans-serif;
    background:#0b1220;color:#cbd5e1}
  .wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;
    height:100%;text-align:center;padding:24px;box-sizing:border-box}
  .dot{width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-bottom:14px}
  .dot.ok{background:#22c55e}
  h1{font-size:15px;margin:0 0 6px;color:#e2e8f0}
  p{margin:4px 0;opacity:.75}
  code{color:#93c5fd}
</style>
</head>
<body>
  <div class="wrap">
    <div class="dot" id="dot"></div>
    <h1>Voitta bridge</h1>
    <p id="status">Connecting to the page…</p>
    <p style="margin-top:14px;font-size:11px;opacity:.5">
      Keep this window open while you use Voitta.<br>Closing it disconnects the assistant.
    </p>
  </div>
  <script src="/bridge/relay.js"></script>
</body>
</html>
"""


# ───────────────────────────────────────────────────────────────────────────
# Relay — runs in the popup (backend origin, no CSP). Executes the locked
# page's network requests same-origin and relays results back.
# ───────────────────────────────────────────────────────────────────────────
_RELAY_JS = r"""(function () {
  "use strict";
  var V = "voitta-bridge";
  var opener = window.opener;
  var statusEl = document.getElementById("status");
  var dotEl = document.getElementById("dot");
  function setStatus(t, ok) {
    if (statusEl) statusEl.textContent = t;
    if (ok && dotEl) dotEl.classList.add("ok");
  }
  if (!opener) {
    setStatus("No opener window — open Voitta from the bookmarklet.");
    return;
  }

  var openerOrigin = null;       // learned from the 'hello' handshake; then pinned
  var sockets = {};              // id -> real WebSocket
  // Verbose frame logging — off unless localStorage.vbridge_debug === "1"
  // (toggleable in the popup's console without a rebuild).
  var DBG = false;
  try { DBG = localStorage.getItem("vbridge_debug") === "1"; } catch (_) {}
  function dpreview(x) {
    try { return typeof x === "string" ? x.slice(0, 240)
      : "[binary " + (x && x.byteLength) + "B]"; } catch (_) { return "?"; }
  }

  function toOpener(msg, transfer) {
    try { opener.postMessage(msg, openerOrigin || "*", transfer || []); }
    catch (e) { /* opener gone */ }
  }

  window.addEventListener("message", function (e) {
    if (e.source !== opener) return;
    var d = e.data;
    if (!d || d.v !== V) return;

    if (d.t === "hello") {
      openerOrigin = e.origin;       // pin to the page that opened us
      setStatus("Connected — loading assistant…", true);
      sendBoot();
      return;
    }
    // After handshake, only accept messages from the pinned origin.
    if (openerOrigin && e.origin !== openerOrigin) return;

    if (d.t === "fetch") handleFetch(d);
    else if (d.t === "ws-open") handleWsOpen(d);
    else if (d.t === "ws-send") {
      var s = sockets[d.id];
      if (DBG) console.log("[vbridge] popup→server", dpreview(d.data));
      if (s && s.readyState === 1) { try { s.send(d.data); } catch (_) {} }
    } else if (d.t === "ws-close") {
      var s2 = sockets[d.id];
      if (s2) { try { s2.close(d.code, d.reason); } catch (_) {} }
    }
  });

  function handleFetch(d) {
    var opts = { method: d.method || "GET", headers: d.headers || {} };
    if (d.body != null && opts.method !== "GET" && opts.method !== "HEAD") {
      opts.body = d.body;            // ArrayBuffer (or null)
    }
    fetch(d.url, opts).then(function (r) {
      return r.arrayBuffer().then(function (buf) {
        var headers = {};
        r.headers.forEach(function (v, k) { headers[k] = v; });
        toOpener({
          v: V, t: "fetch-res", id: d.id, ok: r.ok, status: r.status,
          statusText: r.statusText, headers: headers, url: r.url, body: buf,
        }, [buf]);
      });
    }).catch(function (err) {
      toOpener({ v: V, t: "fetch-res", id: d.id, error: String(err && err.message || err) });
    });
  }

  function handleWsOpen(d) {
    var ws;
    try { ws = d.protocols ? new WebSocket(d.url, d.protocols) : new WebSocket(d.url); }
    catch (err) { toOpener({ v: V, t: "ws-event", id: d.id, event: "error" }); return; }
    ws.binaryType = "arraybuffer";
    sockets[d.id] = ws;
    ws.onopen = function () { toOpener({ v: V, t: "ws-event", id: d.id, event: "open" }); };
    ws.onmessage = function (ev) {
      var data = ev.data, isBinary = false, transfer = [];
      if (data instanceof ArrayBuffer) { isBinary = true; transfer = [data]; }
      if (DBG) console.log("[vbridge] server→popup", dpreview(data));
      toOpener({ v: V, t: "ws-event", id: d.id, event: "message", data: data, isBinary: isBinary }, transfer);
    };
    ws.onclose = function (ev) {
      delete sockets[d.id];
      toOpener({ v: V, t: "ws-event", id: d.id, event: "close",
                 code: ev.code, reason: ev.reason, wasClean: ev.wasClean });
    };
    ws.onerror = function () { toOpener({ v: V, t: "ws-event", id: d.id, event: "error" }); };
  }

  function sendBoot() {
    fetch("/bridge/boot.js").then(function (r) { return r.text(); }).then(function (code) {
      toOpener({ v: V, t: "boot", code: code });
    }).catch(function (err) {
      setStatus("Failed to load assistant: " + err);
    });
  }

  // Re-announce on a short timer until the opener completes the handshake,
  // in case the opener's listener wasn't attached when we first loaded.
  var tries = 0;
  (function ping() {
    if (openerOrigin) return;
    opener.postMessage({ v: V, t: "ready" }, "*");
    if (++tries < 40) setTimeout(ping, 100);
  })();
})();
"""


# ───────────────────────────────────────────────────────────────────────────
# Boot — runs in the locked page (eval'd from source the popup delivers).
# Installs window.fetch + window.WebSocket shims that tunnel backend-bound
# traffic through the popup, then loads the widget bundle.
# ───────────────────────────────────────────────────────────────────────────
_BOOT_JS = r"""(function () {
  "use strict";
  // Idempotency: a second bookmarklet click on an already-booted page must
  // not re-install the shims (that would orphan the running widget). The
  // bookmarklet's own guard normally prevents reaching here twice; this is a
  // belt-and-suspenders backstop.
  if (window.__voittaBridgeBooted) return;
  window.__voittaBridgeBooted = true;

  var V = "voitta-bridge";
  var BACKEND = window.__voittaBackendOrigin;          // e.g. http://127.0.0.1:12359
  var popup = window.__voittaBridgePopup;
  var BACKEND_HOST;
  try { BACKEND_HOST = new URL(BACKEND).host; } catch (_) { BACKEND_HOST = ""; }

  // Verbose frame logging — off unless localStorage.vbridge_debug === "1".
  var DBG = false;
  try { DBG = localStorage.getItem("vbridge_debug") === "1"; } catch (_) {}
  function dpreview(x) {
    try { return typeof x === "string" ? x.slice(0, 240)
      : "[binary " + (x && (x.byteLength || x.size)) + "B]"; } catch (_) { return "?"; }
  }
  function isBackend(url) {
    try { return new URL(url, location.href).host === BACKEND_HOST; }
    catch (_) { return false; }
  }
  function toPopup(msg, transfer) { popup.postMessage(msg, BACKEND, transfer || []); }

  var nextId = 1;
  var pendingFetch = {};   // id -> {resolve, reject}
  var wsById = {};         // id -> BridgeWebSocket

  window.addEventListener("message", function (e) {
    if (e.source !== popup) return;
    var d = e.data;
    if (!d || d.v !== V) return;
    if (d.t === "fetch-res") {
      var p = pendingFetch[d.id];
      if (!p) return;
      delete pendingFetch[d.id];
      if (d.error) { p.reject(new TypeError("Bridge fetch failed: " + d.error)); return; }
      var nullBody = (d.status === 204 || d.status === 205 || d.status === 304);
      var resp = new Response(nullBody ? null : (d.body || null), {
        status: d.status, statusText: d.statusText || "", headers: d.headers || {},
      });
      try { Object.defineProperty(resp, "url", { value: d.url, configurable: true }); } catch (_) {}
      p.resolve(resp);
    } else if (d.t === "ws-event") {
      var s = wsById[d.id];
      if (s) s.__emit(d);
    }
  });

  // ---- fetch shim ---------------------------------------------------------
  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function (input, init) {
    var probe;
    try { probe = (typeof input === "string" || input instanceof URL) ? String(input) : input.url; }
    catch (_) { probe = ""; }
    if (!isBackend(probe)) return realFetch(input, init);

    // Normalise any body shape (string/Blob/FormData/ArrayBuffer) into an
    // ArrayBuffer + headers via the Request constructor — this also folds in
    // multipart boundaries for FormData uploads.
    var req;
    try { req = new Request(input, init); }
    catch (e) { return Promise.reject(e); }
    var headers = {};
    req.headers.forEach(function (v, k) { headers[k] = v; });

    return req.clone().arrayBuffer().then(function (buf) {
      return new Promise(function (resolve, reject) {
        var id = nextId++;
        pendingFetch[id] = { resolve: resolve, reject: reject };
        var hasBody = buf && buf.byteLength > 0;
        toPopup({
          v: V, t: "fetch", id: id, url: req.url, method: req.method,
          headers: headers, body: hasBody ? buf : null,
        }, hasBody ? [buf] : []);
      });
    });
  };

  // ---- WebSocket shim -----------------------------------------------------
  var RealWS = window.WebSocket;
  function BridgeWebSocket(url, protocols) {
    var abs;
    try { abs = new URL(url, location.href); } catch (_) { abs = null; }
    if (!abs || abs.host !== BACKEND_HOST) {
      // Not backend-bound — defer to the real implementation.
      return protocols ? new RealWS(url, protocols) : new RealWS(url);
    }
    this.url = abs.href;
    this.readyState = 0;            // CONNECTING
    this.bufferedAmount = 0;
    this.extensions = "";
    this.protocol = "";
    this.binaryType = "blob";
    this.onopen = this.onclose = this.onmessage = this.onerror = null;
    this.__listeners = {};
    this.__id = nextId++;
    wsById[this.__id] = this;
    toPopup({ v: V, t: "ws-open", id: this.__id, url: abs.href, protocols: protocols || null });
  }
  BridgeWebSocket.CONNECTING = 0;
  BridgeWebSocket.OPEN = 1;
  BridgeWebSocket.CLOSING = 2;
  BridgeWebSocket.CLOSED = 3;

  BridgeWebSocket.prototype.send = function (data) {
    var self = this;
    if (data instanceof Blob) {
      data.arrayBuffer().then(function (b) {
        toPopup({ v: V, t: "ws-send", id: self.__id, data: b, isBinary: true });
      });
      return;
    }
    var isBinary = (data instanceof ArrayBuffer) || ArrayBuffer.isView(data);
    var payload = data;
    if (ArrayBuffer.isView(data)) {
      // Copy out the view's bytes (don't transfer — engine.io reuses buffers).
      payload = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
    }
    if (DBG) console.log("[vbridge] page→popup", dpreview(payload));
    toPopup({ v: V, t: "ws-send", id: this.__id, data: payload, isBinary: isBinary });
  };
  BridgeWebSocket.prototype.close = function (code, reason) {
    if (this.readyState >= 2) return;
    this.readyState = 2;            // CLOSING
    toPopup({ v: V, t: "ws-close", id: this.__id, code: code, reason: reason });
  };
  BridgeWebSocket.prototype.addEventListener = function (type, fn) {
    (this.__listeners[type] = this.__listeners[type] || []).push(fn);
  };
  BridgeWebSocket.prototype.removeEventListener = function (type, fn) {
    var a = this.__listeners[type];
    if (!a) return;
    var i = a.indexOf(fn);
    if (i >= 0) a.splice(i, 1);
  };
  BridgeWebSocket.prototype.dispatchEvent = function () { return true; };
  BridgeWebSocket.prototype.__fire = function (type, ev) {
    var h = this["on" + type];
    if (h) { try { h.call(this, ev); } catch (e) { console.error("[voitta-bridge]", e); } }
    var a = this.__listeners[type];
    if (a) a.slice().forEach(function (fn) {
      try { fn.call(this, ev); } catch (e) { console.error("[voitta-bridge]", e); }
    }, this);
  };
  BridgeWebSocket.prototype.__emit = function (d) {
    if (DBG && d.event === "message") console.log("[vbridge] popup→page", dpreview(d.data));
    if (d.event === "open") {
      this.readyState = 1;
      this.__fire("open", _mkEvent("open"));
    } else if (d.event === "message") {
      var data = d.data;
      if (d.isBinary && data instanceof ArrayBuffer && this.binaryType === "blob") {
        data = new Blob([data]);
      }
      this.__fire("message", _mkMessage(data));
    } else if (d.event === "close") {
      this.readyState = 3;
      delete wsById[this.__id];
      this.__fire("close", _mkClose(d.code, d.reason, d.wasClean));
    } else if (d.event === "error") {
      this.__fire("error", _mkEvent("error"));
    }
  };
  function _mkEvent(type) { try { return new Event(type); } catch (_) { return { type: type }; } }
  function _mkMessage(data) {
    try { return new MessageEvent("message", { data: data }); }
    catch (_) { return { type: "message", data: data }; }
  }
  function _mkClose(code, reason, wasClean) {
    try { return new CloseEvent("close", { code: code, reason: reason, wasClean: wasClean }); }
    catch (_) { return { type: "close", code: code, reason: reason, wasClean: wasClean }; }
  }
  window.WebSocket = BridgeWebSocket;

  // ---- load the widget ----------------------------------------------------
  // Fetched through the (now-shimmed) fetch, so it tunnels via the popup.
  window.fetch(BACKEND + "/widget.js").then(function (r) {
    if (!r.ok) throw new Error("widget.js HTTP " + r.status);
    return r.text();
  }).then(function (src) {
    (0, eval)(src);
  }).catch(function (err) {
    console.error("[voitta-bridge] failed to load widget:", err);
  });
})();
"""


@router.get("/bridge", include_in_schema=False)
async def bridge_page() -> HTMLResponse:
    return HTMLResponse(content=_BRIDGE_HTML, headers={"Cache-Control": "no-store"})


@router.get("/bridge/relay.js", include_in_schema=False)
async def bridge_relay_js() -> Response:
    return Response(content=_RELAY_JS, media_type="application/javascript", headers=_JS_HEADERS)


@router.get("/bridge/boot.js", include_in_schema=False)
async def bridge_boot_js() -> Response:
    return Response(content=_BOOT_JS, media_type="application/javascript", headers=_JS_HEADERS)
