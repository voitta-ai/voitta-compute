"""``edit_script(name, edits)`` — search-replace patches with re-smoke.

The patch is an ordered list of ``{find, replace}`` pairs applied
sequentially. Each ``find`` must occur exactly once in the current
source (no count argument for now — keeps the contract unambiguous;
duplicate matches are an error the model can resolve by providing more
context around ``find``).

The candidate result is smoke-tested; only on success is it persisted.
"""

from __future__ import annotations

from typing import Any

from app.reports import sandbox, store
from app.reports.slug import InvalidSlug, validate_slug
from app.tools.registry import ToolCtx, ToolSpec, registry


def _apply_edits(source: str, edits: list[dict[str, str]]) -> tuple[str, str | None]:
    """Apply the patch list; return ``(new_source, error_or_None)``."""
    out = source
    for i, edit in enumerate(edits):
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not isinstance(replace, str):
            return out, f"edit[{i}] must be {{find: str, replace: str}}"
        if not find:
            return out, f"edit[{i}].find must be non-empty"
        count = out.count(find)
        if count == 0:
            return out, f"edit[{i}].find not present in source"
        if count > 1:
            return out, (
                f"edit[{i}].find matches {count} times — add more context "
                "to make the match unique"
            )
        out = out.replace(find, replace, 1)
    return out, None


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    edits = args.get("edits") or []
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}
    if not store.exists(name):
        return {
            "ok": False,
            "error": (
                f"script {name!r} does not exist. If you just got a "
                f"ValueError or other error from define_script, that "
                f"script was never saved — define_script is "
                f"transactional and writes nothing on failure. "
                f"Call define_script again with the fixed code, not "
                f"edit_script."
            ),
        }
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "`edits` must be a non-empty list"}

    current = store.read_code(name)
    candidate, err = _apply_edits(current, edits)
    if err:
        return {"ok": False, "error": err}

    result = sandbox.smoke_test(name, candidate)
    if not result.ok:
        return {"ok": False, "error": result.error, "traceback": result.traceback}

    meta = store.write_script(name, candidate)
    return {
        "ok": True,
        "name": meta.name,
        "updated_at": meta.updated_at,
        "edits_applied": len(edits),
    }


registry.register(
    ToolSpec(
        name="edit_script",
        description=(
            "Apply ordered search-replace edits to an existing script — "
            "the ONLY way to change an existing script (never delete and "
            "re-create). Each find must match exactly once; the candidate "
            "must pass smoke-test before persistence. Prefer small "
            "targeted edits; for a structural rewrite, a single edit with "
            "find = the entire current source also works."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {"type": "string"},
                            "replace": {"type": "string"},
                        },
                        "required": ["find", "replace"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name", "edits"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
