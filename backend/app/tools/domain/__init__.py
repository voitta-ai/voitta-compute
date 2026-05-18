"""Domain tools — every module registers its tools as a side-effect of
import. Importing the package therefore populates the global registry.

This package holds **provider-agnostic** tools (RAG, web fetch,
screenshot, buffers, scripts, HoloViz reports, python-side storage,
generic page context). Provider-specific tools (Google Drive, future
Dropbox, etc.) live in ``app.tools.providers``.
"""

from app.tools.domain import (  # noqa: F401  — registration side-effects
    rag,
    context,
    dom,
    buffers,
    buffers_arrow,
    python_storage,
    scripts,
    holoviz,
    flow,
    screenshot,
    report_edits,
    theme,
    url_fetch,
    web,
)
