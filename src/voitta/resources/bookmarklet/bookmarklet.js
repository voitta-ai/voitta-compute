// Voitta bookmarklet source — readable form. Minify into a single
// `javascript:` URL and add as a bookmark. The bookmarklet boots the
// in-page widget by injecting a <script src="https://127.0.0.1:12358/widget.js">
// onto the host page. The backend (run with `cd backend && ./run.sh`)
// must be listening on that origin.
//
// Re-clicking the bookmark on a page where the widget is already loaded
// re-mounts it (in case the host SPA navigated and torn the host down).

javascript: (function () {
  const B = "https://127.0.0.1:12358";
  if (window.__voittaBookmarkletLoaded) {
    if (window.VoittaBookmarklet && typeof window.VoittaBookmarklet.mount === "function") {
      window.VoittaBookmarklet.mount();
    }
    return;
  }
  window.__voittaBookmarkletLoaded = true;
  const s = document.createElement("script");
  s.src = B + "/widget.js?t=" + Date.now();
  s.async = true;
  s.onerror = function () {
    window.__voittaBookmarkletLoaded = false;
    alert(
      "Voitta bookmarklet: could not load widget from " + B +
      "\n\nIs the backend running? (cd backend && ./run.sh)"
    );
  };
  document.documentElement.appendChild(s);
})();
