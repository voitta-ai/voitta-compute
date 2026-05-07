"""Google Drive provider — tools active on drive.google.com.

Three layers:

  • ``context.py``     — ``drive_get_page_context``: parses the active
    URL so the LLM knows which folder / search / file the user is
    looking at without prompting. Host-gated via ``host_pattern``.
  • ``tools.py``       — list / search / metadata / download / export +
    pickup tool. The OAuth-backed tools are host-gated AND
    visibility-gated to ``google_oauth.is_connected``; the pickup tool
    has the inverse gate.
  • ``page_scrape.py`` — ``drive_list_visible_files``: DOM-scrape
    fallback for ``drive_list_files`` when OAuth isn't connected.
    Host-gated AND visible only while OAuth is off.
"""

from app.tools.providers.drive import (  # noqa: F401
    context,
    page_scrape,
    tools,
)
