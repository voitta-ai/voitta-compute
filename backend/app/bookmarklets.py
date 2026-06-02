"""Bookmarklet ``javascript:`` string builders, parameterized by backend origin.

Single source of truth shared by the macOS tray (app.desktop) and the server
``GET /bookmarklets`` page. The JS is identical to what the tray emitted before
this module existed — only the origin is now an argument:

* tray  → normal: ``_server_url()`` (https/http at 127.0.0.1:PORT)
         bridge: ``http://127.0.0.1:PLAINTEXT_PORT``
* server → both:  ``VOITTA_PUBLIC_BASE_URL`` (e.g. https://bookmarklet.voitta.ai)

Passing the tray's original origins reproduces the previous strings byte-for-byte,
so the desktop build is unchanged.
"""

from __future__ import annotations


def normal_bookmarklet(origin: str) -> str:
    """``javascript:`` URL that injects ``<script src=origin/widget.js>``.

    For ordinary pages whose CSP allows loading the widget + reaching the
    backend directly.
    """
    url = f"{origin}/widget.js"
    return (
        "javascript:(()=>{"
        f"const u='{url}';"
        "const s=document.createElement('script');"
        "if(window.trustedTypes&&window.trustedTypes.createPolicy){"
        "try{s.src=window.trustedTypes.createPolicy('voitta-inject#'+Math.random(),{createScriptURL:x=>x}).createScriptURL(u);}catch(e){s.src=u;}"
        "}else{s.src=u;}"
        "document.head.appendChild(s);"
        "})();"
    )


def bridge_bookmarklet(origin: str) -> str:
    """Bookmarklet for hardened-CSP pages (e.g. Salesforce Lightning).

    The page's ``script-src``/``connect-src`` block both loading widget.js and
    reaching the backend, so this opens a popup served from ``origin`` and
    bootstraps the widget through it via ``postMessage`` (neither ``window.open``
    nor ``postMessage`` is governed by CSP). See ``app.bridge`` for the
    relay/shim that the popup and page run.
    """
    return (
        "javascript:(()=>{"
        f"const B='{origin}';"
        # Idempotent: if the bridge is already active and its popup is still
        # open, just focus it. Re-opening would reload the popup (same window
        # name), killing the live socket and orphaning the mounted widget.
        "if(window.__voittaBridge&&window.__voittaBridgePopup&&!window.__voittaBridgePopup.closed){try{window.__voittaBridgePopup.focus();}catch(_){}return;}"
        "const p=window.open(B+'/bridge','voitta-bridge','width=440,height=680');"
        "if(!p){alert('Voitta: please allow pop-ups for this site, then click the bookmark again.');return;}"
        "window.__voittaBackendOrigin=B;window.__voittaBridgePopup=p;window.__voittaBridge=true;"
        "window.addEventListener('message',function h(e){"
        "if(e.source!==p||!e.data||e.data.v!=='voitta-bridge')return;"
        "if(e.data.t==='ready'){p.postMessage({v:'voitta-bridge',t:'hello',origin:location.origin},B);}"
        "else if(e.data.t==='boot'){window.removeEventListener('message',h);(0,eval)(e.data.code);}"
        "});"
        "})();"
    )
