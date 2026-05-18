"""Verify the backend accepts replayed tool_use / tool_result history.

Regression guard for the "model pretends to make edits" bug: when the
frontend sends an assistant turn whose `content` is a list of dicts
including tool_use blocks (plus a follow-up user turn with tool_result
blocks), the chat route MUST accept the payload, rebuild messages
verbatim, and not flatten/strip those blocks.

We don't run the agent loop end-to-end (that needs a provider key);
we just confirm the request validation and the messages[] assembly.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.routes.chat import ChatRequest  # noqa: E402


def main() -> int:
    failures = 0

    # The wire payload the FE will now produce after the fix: an
    # assistant turn carrying tool_use blocks, followed by a synthetic
    # user turn carrying tool_result blocks. Both are list-of-dict
    # content, which the old Message.content schema would reject.
    payload = {
        "messages": [
            {"role": "user", "content": "make a smoke report"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll define one."},
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "define_report",
                        "input": {"name": "smoke", "code": "def build(ctx): pass"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": '{"ok": true, "report_id": "smoke"}',
                        "is_error": False,
                    },
                ],
            },
            {"role": "user", "content": "now show it"},
        ],
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "api_key": "sk-test",
    }
    try:
        req = ChatRequest(**payload)
    except Exception as exc:
        print(f"FAIL  ChatRequest rejected replayable history: {exc}")
        return 1

    if len(req.messages) != 4:
        print(f"FAIL  expected 4 messages, got {len(req.messages)}")
        failures += 1
    else:
        print("OK    4 messages accepted")

    # The assistant turn must keep both blocks (text + tool_use).
    assistant = req.messages[1]
    if not isinstance(assistant.content, list):
        print(f"FAIL  assistant content not a list: {type(assistant.content)}")
        failures += 1
    elif len(assistant.content) != 2:
        print(f"FAIL  assistant content lost blocks: {assistant.content}")
        failures += 1
    else:
        types = [
            b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            for b in assistant.content
        ]
        if types != ["text", "tool_use"]:
            print(f"FAIL  assistant block types: {types}")
            failures += 1
        else:
            print("OK    assistant text + tool_use preserved")

    # The tool_result user turn must keep is_error=False and the result.
    tool_user = req.messages[2]
    if not isinstance(tool_user.content, list) or len(tool_user.content) != 1:
        print(f"FAIL  tool_result user content shape wrong: {tool_user.content}")
        failures += 1
    else:
        b = tool_user.content[0]
        b = b if isinstance(b, dict) else b.model_dump()
        if b.get("type") != "tool_result" or b.get("tool_use_id") != "toolu_abc":
            print(f"FAIL  tool_result block lost identity: {b}")
            failures += 1
        else:
            print("OK    tool_result block preserved")

    # And the plain-string fallback still works (back-compat).
    plain = ChatRequest(messages=[{"role": "user", "content": "hi"}],  # type: ignore[arg-type]
                       provider="anthropic", model="x", api_key="k")
    if not isinstance(plain.messages[0].content, str):
        print(f"FAIL  plain-string back-compat broken: {plain.messages[0].content}")
        failures += 1
    else:
        print("OK    plain-string content still accepted")

    if failures:
        print(f"\n{failures} assertion(s) failed")
        return 1
    print("\nchat history replay: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
