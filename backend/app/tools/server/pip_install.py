"""``pip_install(packages)`` — install Python packages into the live
runtime so report/compute scripts can import them mid-session.

Thin wrapper over ``app.installer.pip_install_runtime`` (the same
in-process pip + writable user-site the first-launch installer uses).
The installed package is importable immediately — no app restart.

Auto-install: the LLM calls this autonomously when an import fails with
ModuleNotFoundError. A confirmation / gating layer can be added later by
introducing a ``visibility_check`` or a confirm arg — for now it is a
global, always-available tool.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry


async def _pip_install(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    raw = args.get("packages")
    if isinstance(raw, str):
        raw = [raw]
    specs = [s.strip() for s in (raw or []) if isinstance(s, str) and s.strip()]
    if not specs:
        return {
            "ok": False,
            "error": "invalid_args",
            "message": "packages: a non-empty list of pip specs is required.",
        }

    # pip is synchronous and can run for many seconds — offload to a worker
    # thread so the chat socket's ping/pong keeps flowing (a >20s block can
    # drop the bridge WebSocket).
    from app.installer import pip_install_runtime

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, pip_install_runtime, specs)


registry.register(
    ToolSpec(
        name="pip_install",
        description=(
            "Install Python package(s) into the live runtime so report and "
            "compute scripts can import them. Call this when a script fails "
            "with ModuleNotFoundError, then retry the script — the package is "
            "importable immediately (no restart). Packages persist until the "
            "next app update.\n"
            "NOTE: only packages with prebuilt macOS arm64 / CPython 3.12 "
            "wheels install reliably; source-only packages may fail (the "
            "bundle has no C compiler). Requires internet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "pip requirement specs, e.g. "
                        "['h5py>=3.10', 'soundfile']."
                    ),
                },
            },
            "required": ["packages"],
        },
        handler=_pip_install,
        side="server",
        global_tool=True,
    )
)
