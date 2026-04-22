from app.tools.registry import (
    ToolCtx,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    registry,
)

# Side-effect import: each domain / provider module registers its tools
# onto the global registry on import. Importing these packages therefore
# populates the registry. Import this module anywhere that expects a
# fully-populated registry (e.g. `app.routes.chat`).
from app.tools import domain  # noqa: F401  (registration side-effect)
from app.tools import providers  # noqa: F401

__all__ = ["ToolCtx", "ToolRegistry", "ToolResult", "ToolSpec", "registry"]
