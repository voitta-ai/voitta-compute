"""eBay plugin — DOM-scrape tools for the active eBay tab.

Loaded via voitta core's plugin auto-discovery. Each tool module
calls ``registry.register(...)`` at import time; ``host_pattern`` is
auto-applied from manifest.json (``ebay.com``).
"""

from voitta_ebay import (  # noqa: F401  — registration side-effects
    tools,
)
