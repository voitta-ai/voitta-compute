"""VEED.IO editor plugin — read open project composition and media.

Loaded via voitta core's plugin auto-discovery. Tools are registered
at import time; ``host_patterns: ["www.veed.io"]`` in manifest.json
gates them to veed.io tabs only.
"""

from voitta_veed import tools  # noqa: F401 — registration side-effects
