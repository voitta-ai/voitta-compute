// Image renderer that survives a hardened-site CSP.
//
// On ordinary pages this is just an <img> pointing at the backend. On the
// hardened-site bridge, the page's `img-src` forbids loading images from the
// localhost backend — but it allows `blob:`. So in bridge mode we pull the
// bytes through the (popup-shimmed) `window.fetch` and hand the <img> an
// object URL instead. `data:`/`blob:` sources pass through untouched.

import { useEffect, useState } from "react";

const BRIDGE =
  (window as unknown as { __voittaBridge?: boolean }).__voittaBridge === true;

export function resolveUrl(url: string | undefined, backendOrigin: string): string {
  if (!url) return "";
  if (/^(https?:|data:|blob:)/i.test(url)) return url;
  const base = backendOrigin.replace(/\/$/, "");
  return base + (url.startsWith("/") ? url : "/" + url);
}

interface Props {
  url: string | undefined;
  backendOrigin: string;
  alt?: string;
  className?: string;
}

export default function BridgeImg({ url, backendOrigin, alt, className }: Props) {
  const resolved = resolveUrl(url, backendOrigin);
  const [src, setSrc] = useState(BRIDGE ? "" : resolved);

  useEffect(() => {
    if (!BRIDGE || /^(data:|blob:)/i.test(resolved)) {
      setSrc(resolved);
      return;
    }
    let cancelled = false;
    let objUrl = "";
    window
      .fetch(resolved)
      .then((r) => r.blob())
      .then((b) => {
        if (cancelled) return;
        objUrl = URL.createObjectURL(b);
        setSrc(objUrl);
      })
      .catch((err) => console.warn("[voitta] bridge image load failed", err));
    return () => {
      cancelled = true;
      if (objUrl) URL.revokeObjectURL(objUrl);
    };
  }, [resolved]);

  if (!src) return null;
  return <img className={className} src={src} alt={alt || ""} />;
}
