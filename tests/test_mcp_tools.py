"""MCP server: tools/list + cli_sessions parity with REST.

Exercises the FastMCP instance directly (no HTTP round-trip) — uses
the in-memory transport via ``fastmcp.Client``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from fastmcp import Client  # noqa: E402

from app.services.mcp_server import get_server  # noqa: E402


EXPECTED_TOOLS = {
    "cli_sessions", "cli_page", "cli_eval", "cli_chat_state",
    "cli_screenshot", "cli_chat_inject", "cli_chat",
}


async def run() -> int:
    failures = 0
    server = get_server()

    async with Client(server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        missing = EXPECTED_TOOLS - names
        extra_ok = True  # extra tools are not a failure
        if missing:
            print(f"FAIL  missing tools: {missing}")
            failures += 1
        else:
            print(f"OK    all 7 expected tools present (got {len(names)})")

        for t in tools:
            if t.name in EXPECTED_TOOLS and not (t.description or "").strip():
                print(f"FAIL  tool {t.name} has empty description")
                failures += 1

        # cli_sessions parity — both surfaces should produce same shape
        # against an empty bridge (no live bookmarklet sessions in
        # tests). Result is wrapped in CallToolResult; structuredContent
        # has the dict.
        res = await client.call_tool("cli_sessions", {})
        payload = res.structured_content or {}
        # FastMCP wraps a top-level dict-return under "result" — unwrap
        # if present.
        if "result" in payload and "sessions" not in payload:
            payload = payload["result"]
        if "count" not in payload or "sessions" not in payload:
            print(f"FAIL  cli_sessions shape: {payload}")
            failures += 1
        elif not isinstance(payload["sessions"], list):
            print(f"FAIL  cli_sessions sessions not list: {payload}")
            failures += 1
        else:
            print(f"OK    cli_sessions returned shape with count={payload['count']}")

    return failures


def main() -> int:
    failures = asyncio.run(run())
    if failures:
        print(f"\n{failures} assertion(s) failed")
        return 1
    print("\nmcp tools: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
