"""Google Drive provider — tools active on drive.google.com.

Two layers:

  • ``context.py`` — ``drive_get_page_context``: parses the active URL
    so the LLM knows which folder / search / file the user is looking
    at without prompting. Host-gated via ``host_pattern``.
  • ``tools.py``   — list / search / metadata / download / export
    tools. Host-gated AND visibility-gated (only exposed once the
    user has connected the Google OAuth flow from Settings).
"""

from app.tools.providers.drive import (  # noqa: F401
    context,
    tools,
)
