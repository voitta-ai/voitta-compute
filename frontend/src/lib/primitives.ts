// Generic browser primitives the server can dispatch via the bridge.
// Provider-agnostic; provider-specific primitives live with their
// provider in `app.tools.providers`.

import { getBackendOrigin, PrimitiveError, registerPrimitive } from "./bridge";
import { getActiveReportIframe, getActiveReportInfo } from "./report-iframe";

const DOM_CAP = 200_000;

// ---- get_url ---------------------------------------------------------------

registerPrimitive("get_url", async () => ({
  href: location.href,
  pathname: location.pathname,
  search: location.search,
  hash: location.hash,
  title: document.title,
}));

// ---- download_in_modal -----------------------------------------------------
//
// Used by the Drive pickup fallback. Opens the URL in an iframe inside
// a modal overlay rendered in our Shadow DOM. Why iframe-in-modal:
//
//   • Drive serves the "couldn't scan for viruses" interstitial at
//     drive.usercontent.google.com — a regular HTML page with a
//     "Download anyway" form button. Drive deliberately blocks
//     programmatic completion (anti-abuse), so the user must click
//     the button themselves.
//
//   • Both drive.google.com/uc?export=download and the
//     drive.usercontent.google.com redirect target allow framing —
//     verified at runtime; no X-Frame-Options or CSP frame-ancestors.
//     So we can embed the interstitial in our own chrome.
//
//   • The iframe stays on the user's current Drive folder tab — no
//     popup, no tab switch. When the user clicks "Download anyway"
//     the form posts inside the iframe, Drive responds with
//     Content-Disposition: attachment, browser detaches as a download.
//     File lands in Downloads, watcher catches it as before.
//
//   • For small files served directly: Chrome immediately detaches
//     the iframe response as a download, the iframe shows a blank
//     page momentarily, modal stays open until the user dismisses.
//     Acceptable — file already arrived.

interface ModalElements {
  root: HTMLElement;
  backdrop: HTMLDivElement;
  panel: HTMLDivElement;
  iframe: HTMLIFrameElement;
}

// Resolve the bookmarklet's Shadow root if it's installed on the page,
// otherwise fall back to ``document.body``. We render the modal inside
// the Shadow DOM whenever possible so it inherits the host's
// ``z-index: 2147483647`` and sits above the chat drawer (also in the
// Shadow DOM). At document body level the host wins the z-index race
// and the modal appears underneath the drawer.
function _modalParent(): { parent: ParentNode; pointerEvents: string } {
  const HOST_ID = "voitta-bookmarklet-host";
  const host = document.getElementById(HOST_ID) as HTMLElement | null;
  if (host && host.shadowRoot) {
    // The host has ``pointer-events:none`` so background clicks reach
    // the host page. Re-enable it on our modal root so the backdrop /
    // close button respond.
    return { parent: host.shadowRoot, pointerEvents: "auto" };
  }
  return { parent: document.body, pointerEvents: "auto" };
}

function buildDownloadModal(
  url: string,
  title: string,
  modalId: string,
): ModalElements {
  const root = document.createElement("div");
  root.setAttribute("data-voitta-modal", "drive-download");
  // ``data-modal-id`` is what ``close_download_modal`` looks up when
  // the backend calls in to dismiss after a successful watcher pickup.
  root.setAttribute("data-modal-id", modalId);
  root.style.cssText =
    "position:fixed;inset:0;z-index:2147483647;pointer-events:auto;";

  const backdrop = document.createElement("div");
  backdrop.style.cssText = (
    "position:absolute;inset:0;background:rgba(8,12,20,0.55);" +
    "backdrop-filter:blur(2px);"
  );
  root.appendChild(backdrop);

  const panel = document.createElement("div");
  panel.style.cssText = (
    "position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);" +
    "width:min(720px, 92vw);height:min(540px, 80vh);" +
    "background:#fff;border-radius:8px;" +
    "box-shadow:0 24px 64px rgba(0,0,0,0.45);" +
    "display:flex;flex-direction:column;overflow:hidden;" +
    "font:13px/1.4 system-ui, -apple-system, sans-serif;color:#0f172a;"
  );
  root.appendChild(panel);

  // Header — explains why this popup exists. Drive's interstitial
  // gives users no context about who opened it; without our chrome
  // they'd be confused.
  const header = document.createElement("div");
  header.style.cssText = (
    "display:flex;align-items:center;gap:10px;padding:10px 14px;" +
    "background:#0f172a;color:#fff;border-bottom:1px solid #1e293b;"
  );
  const titleEl = document.createElement("div");
  titleEl.style.cssText = "flex:1;font-weight:600;font-size:13px;";
  titleEl.textContent = title;
  const hint = document.createElement("div");
  hint.style.cssText = "color:#94a3b8;font-size:11px;";
  hint.textContent = "Click \"Download anyway\" inside ↓";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.textContent = "×";
  closeBtn.setAttribute("aria-label", "Close download dialog");
  closeBtn.style.cssText = (
    "background:transparent;border:1px solid rgba(255,255,255,0.25);" +
    "color:#fff;width:28px;height:24px;border-radius:4px;" +
    "font:16px/1 system-ui;cursor:pointer;padding:0;"
  );
  header.appendChild(titleEl);
  header.appendChild(hint);
  header.appendChild(closeBtn);
  panel.appendChild(header);

  const iframe = document.createElement("iframe");
  iframe.src = url;
  iframe.style.cssText = "flex:1;width:100%;border:0;background:#fff;";
  // Allow downloads from the framed origin. `allow-same-origin` is
  // omitted on purpose — we don't need same-origin access from inside
  // the iframe, and dropping it tightens the sandbox a notch.
  iframe.setAttribute(
    "sandbox",
    "allow-forms allow-scripts allow-popups allow-downloads allow-top-navigation-by-user-activation",
  );
  panel.appendChild(iframe);

  // Footer — manual fallback link in case Drive shows a "wait" page or
  // the iframe never lands.
  const footer = document.createElement("div");
  footer.style.cssText = (
    "padding:8px 14px;background:#f1f5f9;border-top:1px solid #e2e8f0;" +
    "font-size:11px;color:#475569;display:flex;align-items:center;gap:8px;"
  );
  footer.innerHTML = (
    'Stuck? <a href="' + url +
    '" target="_blank" rel="noopener noreferrer" style="color:#2563eb;">' +
    'open in a new tab</a>'
  );
  panel.appendChild(footer);

  // Dismissal: × button or Esc only — backdrop clicks no-op so a
  // misplaced click on the dimmed area outside the iframe doesn't
  // abort an in-flight transfer.
  //
  // Both dismissal paths POST to /api/drive-pickup/cancel so the
  // backend watcher exits immediately with ``error: 'user_cancelled'``
  // instead of stalling for the full 120 s timeout.
  function close(reason: "x" | "esc") {
    root.remove();
    document.removeEventListener("keydown", onKey);
    const origin = getBackendOrigin();
    if (origin) {
      void fetch(origin.replace(/\/$/, "") + "/api/drive-pickup/cancel", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ modal_id: modalId, reason }),
        keepalive: true,
      }).catch(() => {});
    }
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") close("esc");
  }
  closeBtn.addEventListener("click", () => close("x"));
  document.addEventListener("keydown", onKey);

  return { root, backdrop, panel, iframe };
}

registerPrimitive("download_in_modal", async (rawArgs) => {
  const url = String(rawArgs.url ?? "");
  if (!url) throw new PrimitiveError("invalid_args", "url is required");
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new PrimitiveError("invalid_url", `not a valid URL: ${url}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new PrimitiveError(
      "unsupported_scheme",
      `only http(s) URLs allowed; got ${parsed.protocol}`,
    );
  }
  const modalId = String(rawArgs.modal_id ?? "");
  if (!modalId) throw new PrimitiveError("invalid_args", "modal_id is required");
  const filename = String(rawArgs.filename ?? "");
  const title = filename
    ? `Download from Drive — ${filename}`
    : "Download from Drive";

  // Tear down any stale modal with the same id before opening — guards
  // against a re-trigger from the LLM that didn't go through the
  // close_download_modal primitive (e.g. timeout retry path).
  const stale = _findModalById(modalId);
  if (stale) stale.remove();

  const modal = buildDownloadModal(url, title, modalId);
  const { parent } = _modalParent();
  parent.appendChild(modal.root);
  return { ok: true, url, modal_id: modalId, filename: filename || null };
});

// ---- close_download_modal --------------------------------------------------
//
// Server calls this from the pickup tool once the watcher catches the
// downloaded file (or otherwise wants to dismiss the modal — e.g. before
// surfacing an error envelope). Returns ``{ closed }`` so the caller can
// log whether a modal was actually present.

function _findModalById(modalId: string): HTMLElement | null {
  const sel = `[data-voitta-modal="drive-download"][data-modal-id="${
    CSS.escape(modalId)
  }"]`;
  const HOST_ID = "voitta-bookmarklet-host";
  const host = document.getElementById(HOST_ID) as HTMLElement | null;
  if (host && host.shadowRoot) {
    const inShadow = host.shadowRoot.querySelector(sel);
    if (inShadow) return inShadow as HTMLElement;
  }
  const inDoc = document.querySelector(sel);
  return inDoc as HTMLElement | null;
}

registerPrimitive("close_download_modal", async (rawArgs) => {
  const modalId = String(rawArgs.modal_id ?? "");
  if (!modalId) throw new PrimitiveError("invalid_args", "modal_id is required");
  const el = _findModalById(modalId);
  if (!el) return { closed: false, modal_id: modalId };
  el.remove();
  return { closed: true, modal_id: modalId };
});

// ---- trigger_download ------------------------------------------------------
//
// Used by the Drive pickup fallback. Opens the URL in a NEW tab via
// `<a target="_blank" download>` + click(). Why a new tab:
//
//   • Drive serves >25 MB / "couldn't be virus-scanned" files behind
//     an interstitial HTML page (drive.usercontent.google.com/download
//     with a "Download anyway" button). Drive deliberately blocks
//     programmatic completion of that page — the user MUST click the
//     button. Same-tab navigation yanks them out of the folder; an
//     iframe loads the page invisibly and can't be interacted with.
//
//   • Single-account setups with files small enough to skip the
//     interstitial: the new tab opens, Chrome sees Content-Disposition:
//     attachment, downloads the file, closes the now-empty tab itself.
//     Net effect identical to silent download.
//
//   • Cross-origin redirects (authuser=N → drive.usercontent.google.com)
//     don't strip the `download` attribute when the navigation is in
//     a fresh tab.
//
// The popup-blocker concern: Chromium-family browsers permit
// anchor.click() with `target="_blank"` from script as long as the
// originating tab has had recent user activity (the bookmarklet
// installation click counts for several minutes). Firefox is similar.
// Safari is the most restrictive — there the `download` attribute
// takes effect even without target="_blank" via the same-tab path.
//
// We return `opened` so the caller can include "click this URL if
// nothing opened" instructions in the response.

registerPrimitive("trigger_download", async (rawArgs) => {
  const url = String(rawArgs.url ?? "");
  if (!url) throw new PrimitiveError("invalid_args", "url is required");
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new PrimitiveError("invalid_url", `not a valid URL: ${url}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new PrimitiveError(
      "unsupported_scheme",
      `only http(s) URLs allowed; got ${parsed.protocol}`,
    );
  }
  const filename = String(rawArgs.filename ?? "");

  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  if (filename) a.download = filename;
  else a.setAttribute("download", "");
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    if (a.parentNode) a.parentNode.removeChild(a);
  }, 1000);

  return { ok: true, url, filename: filename || null };
});

// ---- open_url_in_new_tab ---------------------------------------------------
//
// Used by the Drive pickup fallback (when OAuth isn't connected). Pops
// `url` in a new tab so the user's existing Google session handles the
// download. Returns whether window.open succeeded — popup blockers can
// intercept this when the call isn't tied to a user gesture, in which
// case we surface the failure so the caller can ask the user to allow
// pop-ups for this origin.

registerPrimitive("open_url_in_new_tab", async (rawArgs) => {
  const url = String(rawArgs.url ?? "");
  if (!url) throw new PrimitiveError("invalid_args", "url is required");
  // Restrict to http/https — refuse javascript:/data: URIs even though
  // the backend won't generate them, since this primitive can be invoked
  // from anywhere on the bridge.
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new PrimitiveError("invalid_url", `not a valid URL: ${url}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new PrimitiveError(
      "unsupported_scheme",
      `only http(s) URLs allowed; got ${parsed.protocol}`,
    );
  }
  const win = window.open(url, "_blank", "noopener,noreferrer");
  if (!win) {
    throw new PrimitiveError(
      "popup_blocked",
      "browser blocked window.open — allow pop-ups for this origin and retry",
    );
  }
  return { ok: true, url };
});

// ---- read_selection --------------------------------------------------------

registerPrimitive("read_selection", async () => {
  const text = window.getSelection()?.toString() ?? "";
  return { text };
});

// ---- read_dom --------------------------------------------------------------

registerPrimitive("read_dom", async (args) => {
  const selector = String(args.selector ?? "");
  const kind = args.kind === "html" ? "html" : "text";
  if (!selector) throw new PrimitiveError("invalid_args", "selector is required");
  let el: Element | null;
  try {
    el = document.querySelector(selector);
  } catch (err) {
    throw new PrimitiveError("invalid_selector", String((err as Error).message));
  }
  if (!el) throw new PrimitiveError("not_found", `no element matches ${selector}`);
  const value =
    kind === "html"
      ? el.outerHTML
      : (el as HTMLElement).innerText ?? el.textContent ?? "";
  if (value.length > DOM_CAP) {
    throw new PrimitiveError("too_large", `${value.length} > ${DOM_CAP}`, {
      size: value.length,
    });
  }
  return { value, kind };
});

// ─────────────────────────────────────────────────────────────────────────
// screenshot_report — rasterise the active HoloViz Panel report iframe.
// Returns a base64 dataURL covering the entire scrollHeight, not just
// the visible viewport. The actual rasterisation runs INSIDE the iframe
// (via html2canvas loaded by the Panel shim) so it has same-origin
// access to all canvases. We just shuttle a postMessage round-trip.
// ─────────────────────────────────────────────────────────────────────────

interface ScreenshotArgs {
  scale?: number;
  quality?: number;
  format?: "webp" | "png";
  timeout_ms?: number;
}

interface ScreenshotResponse {
  type: "voitta_report_event";
  kind: "screenshot_response";
  requestId: string;
  ok: boolean;
  message?: string;
  dataUrl?: string;
  width?: number;
  height?: number;
  full_width?: number;
  full_height?: number;
  scale?: number;
  format?: string;
}

let screenshotCounter = 0;

registerPrimitive("screenshot_report", async (rawArgs) => {
  const args = (rawArgs || {}) as ScreenshotArgs;
  const iframe = getActiveReportIframe();
  if (!iframe || !iframe.contentWindow) {
    throw new PrimitiveError("no_report", "no report is currently open");
  }
  const info = getActiveReportInfo();
  // Block screenshots in edit mode: html2canvas chokes on Panel's
  // editable template CSS (notably the ``color-mix()`` box-shadow on
  // ``.muuri-grid-item``), throwing "unsupported color function". The
  // edit-mode handles also visually clutter the captured image. Exit
  // edit mode first, then retry. iframe.src is preferred over the
  // cached info.url because ReportPane mutates iframe.src on toggle
  // without re-pushing info.
  const url = iframe.src || info?.url || "";
  if (/[?&]editable=true(?:&|$|#)/.test(url)) {
    throw new PrimitiveError(
      "edit_mode",
      "screenshot_report does not work while the report is in edit mode (the editable template uses CSS that html2canvas cannot rasterise). Ask the user to leave edit mode, then retry.",
    );
  }
  const requestId = `ss_${Date.now().toString(36)}_${++screenshotCounter}`;
  const scale = typeof args.scale === "number" ? args.scale : 1;
  const quality = typeof args.quality === "number" ? args.quality : 0.85;
  const format = args.format === "png" ? "png" : "webp";
  const timeout = typeof args.timeout_ms === "number" ? args.timeout_ms : 60_000;

  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener("message", onMessage);
      reject(
        new PrimitiveError(
          "timeout",
          `report screenshot did not return within ${timeout}ms`,
        ),
      );
    }, timeout);

    function onMessage(e: MessageEvent) {
      const data = e.data as ScreenshotResponse | null;
      if (
        !data ||
        typeof data !== "object" ||
        data.type !== "voitta_report_event" ||
        data.kind !== "screenshot_response" ||
        data.requestId !== requestId
      ) {
        return;
      }
      // Only accept from our own iframe.
      if (e.source !== iframe!.contentWindow) return;
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      if (!data.ok) {
        reject(
          new PrimitiveError(
            "screenshot_failed",
            data.message || "iframe screenshot returned ok=false",
          ),
        );
        return;
      }
      resolve({
        ok: true,
        data_url: data.dataUrl,
        width: data.width,
        height: data.height,
        full_width: data.full_width,
        full_height: data.full_height,
        scale: data.scale,
        format: data.format,
        report: info
          ? { report_id: info.report_id, url: info.url, title: info.title }
          : null,
      });
    }
    window.addEventListener("message", onMessage);
    iframe.contentWindow!.postMessage(
      {
        voittaAction: "screenshot",
        requestId,
        scale,
        quality,
        format,
      },
      "*",
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────
// get_report_edits — read the live edit state of the report iframe.
//
// 100% client-side. The iframe holds the truth (Muuri grid + Bokeh
// document + our shim's selection state); the shim posts back a
// snapshot. Three return shapes:
//
//   { status: "no_active_report", message }
//     — no iframe mounted, or the URL doesn't have ?editable=true
//
//   { status: "active_no_edits", report_id, message }
//     — iframe is in edit mode but every card is at default
//       width (100%) / height (Bokeh-natural) / visible, original
//       order, and nothing is selected
//
//   { status: "active", report_id, elements, selected_id, order_changed }
//     — anything else; `elements[]` carries name/title/index/width_pct/
//       height_px/visible/selected for each card. `name` is the Python
//       `name=` argument the script set (null when unset — the LLM is
//       expected to set names on each main component for stable refs).
// ─────────────────────────────────────────────────────────────────────────

interface EditsElement {
  index: number;
  id: string;
  name: string | null;
  title: string | null;
  width_pct: number;
  height_px: number | null;
  visible: boolean;
  selected: boolean;
}

interface EditsResponse {
  type: "voitta_report_event";
  kind: "edits_response";
  requestId: string;
  ok: boolean;
  message?: string;
  elements?: EditsElement[];
  selected_id?: string | null;
  order_changed?: boolean;
}

let editsCounter = 0;

registerPrimitive("get_report_edits", async () => {
  const iframe = getActiveReportIframe();
  if (!iframe || !iframe.contentWindow) {
    return {
      status: "no_active_report",
      message: "no report is currently open in the iframe pane",
    };
  }
  const info = getActiveReportInfo();
  // Live ``iframe.src`` is the source of truth: ReportPane updates it
  // in-place when the user toggles edit mode, but the cached
  // ``info.url`` (set on mount) doesn't get re-pushed. Reading the
  // iframe directly catches the toggle without needing a ReportPane
  // change.
  const url = iframe.src || info?.url || "";
  if (!/[?&]editable=true(?:&|$|#)/.test(url)) {
    return {
      status: "no_active_report",
      message:
        "the active report is not in edit mode (open the report and toggle the edit affordance)",
    };
  }

  const requestId = `ge_${Date.now().toString(36)}_${++editsCounter}`;
  const response: EditsResponse = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener("message", onMessage);
      reject(
        new PrimitiveError(
          "timeout",
          "report iframe did not respond to getEdits within 5s",
        ),
      );
    }, 5_000);
    function onMessage(e: MessageEvent) {
      const data = e.data as EditsResponse | null;
      if (
        !data ||
        typeof data !== "object" ||
        data.type !== "voitta_report_event" ||
        data.kind !== "edits_response" ||
        data.requestId !== requestId
      ) {
        return;
      }
      if (e.source !== iframe!.contentWindow) return;
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(data);
    }
    window.addEventListener("message", onMessage);
    iframe.contentWindow!.postMessage(
      { voittaAction: "getEdits", requestId },
      "*",
    );
  });

  if (!response.ok) {
    return {
      status: "no_active_report",
      message:
        response.message ||
        "report iframe responded but reported no editable grid",
    };
  }

  const elements = response.elements || [];
  const reportId = info?.report_id ?? null;
  const selectedId = response.selected_id ?? null;
  const orderChanged = !!response.order_changed;

  const anyResize = elements.some(
    (el) => el.width_pct !== 100 || el.height_px != null,
  );
  const anyHidden = elements.some((el) => !el.visible);
  const anySelection = selectedId != null;

  if (!anyResize && !anyHidden && !anySelection && !orderChanged) {
    return {
      status: "active_no_edits",
      report_id: reportId,
      message:
        "report is open in edit mode but the user hasn't moved, resized, hidden, or selected anything yet",
    };
  }

  return {
    status: "active",
    report_id: reportId,
    elements,
    selected_id: selectedId,
    order_changed: orderChanged,
  };
});

// ─────────────────────────────────────────────────────────────────────────
// CLI back-channel primitives — invoked by the FastAPI `/cli/*` routes
// from local automation (Claude Code, curl, scripts). Deliberately NOT
// exposed to the in-pane LLM (no ToolSpec wraps them). They give the
// developer a way to drive the bookmarked page from outside the chat UI.
// ─────────────────────────────────────────────────────────────────────────

// Single-round-trip page dump: URL + title + full outerHTML. We use a
// dedicated primitive (rather than chaining get_url + read_dom) because
// `read_dom` enforces a 200 KB cap suited to LLM consumption. Real-world
// pages routinely exceed that, and Claude Code handles big payloads
// fine, so this primitive is uncapped.
registerPrimitive("get_page_dump", async () => ({
  url: location.href,
  title: document.title,
  pathname: location.pathname,
  search: location.search,
  hash: location.hash,
  html: document.documentElement?.outerHTML ?? "",
  user_agent: navigator.userAgent,
  ts: Date.now(),
}));

// JSON-safe serialiser. Faithfully encodes things JSON.stringify can't:
// undefined, BigInt, Symbol, functions, Errors, DOM nodes, Map/Set,
// Date, RegExp, ArrayBuffer/TypedArray, and cyclic graphs. Each
// non-plain value is wrapped in `{__type, ...}` so the consumer can
// reconstruct intent. Plain JSON values pass through untouched.
function _serialize(v: unknown): unknown {
  const seen = new WeakSet<object>();
  function walk(x: unknown): unknown {
    if (x === undefined) return { __type: "undefined" };
    if (x === null) return null;
    const t = typeof x;
    if (t === "string" || t === "boolean") return x;
    if (t === "number") {
      const n = x as number;
      if (!Number.isFinite(n)) return { __type: "Number", value: String(n) };
      return n;
    }
    if (t === "bigint") return { __type: "BigInt", value: String(x) };
    if (t === "symbol") {
      const s = x as symbol;
      return { __type: "Symbol", description: s.description ?? null };
    }
    if (t === "function") {
      const fn = x as { name?: string; toString(): string };
      let src = "";
      try {
        src = fn.toString();
      } catch {
        src = "[unsourceable]";
      }
      return {
        __type: "Function",
        name: fn.name || "(anonymous)",
        source: src.slice(0, 4096),
      };
    }
    if (t !== "object") return String(x);

    const o = x as object;
    if (seen.has(o)) return { __type: "Cycle" };
    seen.add(o);

    if (typeof Element !== "undefined" && o instanceof Element) {
      const el = o as Element;
      let outer = "";
      try {
        outer = el.outerHTML.slice(0, 8192);
      } catch {
        /* some shadow nodes throw */
      }
      return {
        __type: "Element",
        tagName: el.tagName,
        id: el.id || null,
        className:
          typeof el.className === "string" ? el.className : null,
        attributes: Array.from(el.attributes).map((a) => ({
          name: a.name,
          value: a.value,
        })),
        outerHTML: outer,
        textContent: (el.textContent || "").slice(0, 2048),
      };
    }
    if (typeof Node !== "undefined" && o instanceof Node) {
      const n = o as Node;
      return {
        __type: "Node",
        nodeName: n.nodeName,
        nodeValue: n.nodeValue,
      };
    }
    if (o instanceof Error) {
      return {
        __type: "Error",
        name: o.name,
        message: o.message,
        stack: o.stack ?? null,
      };
    }
    if (Array.isArray(o)) return o.map(walk);
    if (o instanceof Map) {
      return {
        __type: "Map",
        entries: Array.from(o.entries()).map(([k, vv]) => [walk(k), walk(vv)]),
      };
    }
    if (o instanceof Set) {
      return {
        __type: "Set",
        values: Array.from(o.values()).map(walk),
      };
    }
    if (o instanceof Date) {
      return { __type: "Date", value: o.toISOString() };
    }
    if (o instanceof RegExp) {
      return { __type: "RegExp", source: o.source, flags: o.flags };
    }
    if (o instanceof ArrayBuffer) {
      return { __type: "ArrayBuffer", byteLength: o.byteLength };
    }
    if (ArrayBuffer.isView(o)) {
      const tav = o as ArrayBufferView & { length?: number };
      return {
        __type: o.constructor?.name ?? "TypedArray",
        byteLength: tav.byteLength,
        length: tav.length ?? null,
      };
    }
    // Plain / unknown object — walk own enumerable string keys.
    const out: Record<string, unknown> = {};
    let keys: string[];
    try {
      keys = Object.keys(o as Record<string, unknown>);
    } catch {
      return { __type: "OpaqueObject", constructor: o.constructor?.name ?? null };
    }
    for (const k of keys) {
      try {
        out[k] = walk((o as Record<string, unknown>)[k]);
      } catch (e) {
        out[k] = {
          __type: "AccessError",
          message: String((e as Error)?.message ?? e),
        };
      }
    }
    if (o.constructor && o.constructor.name && o.constructor.name !== "Object") {
      out.__constructor = o.constructor.name;
    }
    return out;
  }
  return walk(v);
}

interface EvalArgs {
  js?: string;
  await_ms?: number;
}

registerPrimitive("eval_js", async (rawArgs) => {
  const args = rawArgs as EvalArgs;
  const src = String(args.js ?? "");
  if (!src) throw new PrimitiveError("invalid_args", "js is required");
  const awaitMs =
    typeof args.await_ms === "number" && args.await_ms > 0
      ? args.await_ms
      : 30_000;

  // Capture console.log/info/warn/error/debug for the duration of the
  // eval. We record level + serialised args + timestamp; restore after.
  const captured: Array<{ level: string; args: unknown[]; ts: number }> = [];
  const levels = ["log", "info", "warn", "error", "debug"] as const;
  type ConsoleLevel = (typeof levels)[number];
  const orig: Partial<Record<ConsoleLevel, (...a: unknown[]) => void>> = {};
  for (const lv of levels) {
    orig[lv] = console[lv].bind(console);
    console[lv] = (...a: unknown[]) => {
      try {
        captured.push({
          level: lv,
          args: a.map((x) => _serialize(x)),
          ts: Date.now(),
        });
      } catch {
        /* must not break console */
      }
      return orig[lv]!(...a);
    };
  }

  const t0 = performance.now();
  let value: unknown = undefined;
  let errorObj: { name: string; message: string; stack: string | null } | null =
    null;
  let timedOut = false;

  try {
    const AsyncFunction = Object.getPrototypeOf(async function () {})
      .constructor as new (...a: string[]) => (...a: unknown[]) => Promise<unknown>;
    const fn = new AsyncFunction(src);
    const result = fn.call(window);
    const timeout = new Promise<never>((_, reject) => {
      setTimeout(
        () => reject(new Error(`eval_js await_ms timeout after ${awaitMs}ms`)),
        awaitMs,
      );
    });
    value = await Promise.race([result, timeout]);
  } catch (e) {
    const err = e as { name?: string; message?: string; stack?: string };
    errorObj = {
      name: err?.name || "Error",
      message: err?.message || String(e),
      stack: err?.stack ?? null,
    };
    if (errorObj.message.includes("await_ms timeout")) timedOut = true;
  } finally {
    for (const lv of levels) {
      if (orig[lv]) console[lv] = orig[lv]!;
    }
  }
  const elapsed_ms = Math.round(performance.now() - t0);
  return {
    ok: errorObj === null,
    value: _serialize(value),
    console: captured,
    elapsed_ms,
    timed_out: timedOut,
    error: errorObj,
  };
});

