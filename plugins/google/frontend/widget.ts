// Google plugin — frontend primitives.
//
// Side-effect import. Voitta core's widget.tsx globs every plugin's
// frontend/widget.ts and bundles them. Each plugin can register its
// own browser primitives via the same registerPrimitive() the core
// uses; primitive names live in a flat namespace, so plugin authors
// should pick distinctive names.
//
// What we register:
//   • download_in_modal      — embed a download URL in a Shadow-DOM
//                               iframe modal so Drive interstitials
//                               render in-page.
//   • close_download_modal   — backend-initiated dismissal.
//   • trigger_download       — fallback: anchor-click + new tab.
//   • open_url_in_new_tab    — generic helper used by older code paths.
//
// All four are Google-Drive-specific in their use cases, but the JS is
// generic enough that other plugins could call them too.

import {
  getBackendOrigin,
  PrimitiveError,
  registerPrimitive,
} from "../../../frontend/src/lib/bridge";

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
function _resolveShadowRoot(): ShadowRoot | null {
  // The shadow is attached in ``mode: "closed"`` so ``host.shadowRoot``
  // returns null from outside the original attachShadow call. Voitta
  // core stashes a getter at ``window.VoittaBookmarklet.getShadowRoot``
  // for in-bundle code to use.
  const w = window as unknown as {
    VoittaBookmarklet?: { getShadowRoot?: () => ShadowRoot };
  };
  const root = w.VoittaBookmarklet?.getShadowRoot?.();
  return root ?? null;
}

function _modalParent(): { parent: ParentNode; pointerEvents: string } {
  const root = _resolveShadowRoot();
  if (root) {
    return { parent: root, pointerEvents: "auto" };
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
        credentials: "include",
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
  const root = _resolveShadowRoot();
  if (root) {
    const inShadow = root.querySelector(sel);
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
