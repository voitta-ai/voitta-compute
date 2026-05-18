"""LinkedIn plugin — DOM-scrape tools for the active LinkedIn tab.

Loaded via voitta core's plugin auto-discovery. Each tool module
calls ``registry.register(...)`` at import time; ``host_pattern`` is
auto-applied from manifest.json (``linkedin.com``).
"""

from voitta_linkedin import (  # noqa: F401  — registration side-effects
    tools,
)
