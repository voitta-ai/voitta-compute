import { mount } from "./widget";

// Auto-mount when the bundle is loaded from the bookmarklet. The mount
// function is idempotent — calling it twice is a no-op.
mount();

// Expose a tiny manual handle for power users / debugging from devtools.
// (logger.ts also writes log + logSnapshot onto this same global; both
// declarations have to agree on the union type.)
window.VoittaBookmarklet = { ...(window.VoittaBookmarklet || {}), mount };
