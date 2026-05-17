"""Voitta Enterprise portal plugin.

Loaded via voitta core's plugin auto-discovery. Tools are registered
at import time by the tools sub-module; ``host_pattern`` is applied
automatically from manifest.json (``enterprise.voitta.ai``).
"""

from voitta_enterprise import (  # noqa: F401  — registration side-effects
    tools,
    resolver,  # registers the vre:// scheme via ensure_local.register
)
