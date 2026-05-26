"""Single-renderer module — HTML only.

Scripts return a raw HTML string from ``build()``. We inject the
screenshot shim into ``<head>`` and cache the assembled document.
There is no other renderer. There is no kind detection.
"""

from app.reports.renderers.html import (  # noqa: F401
    get_cached,
    render_html,
)
