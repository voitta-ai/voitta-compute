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

from app.reports import sandbox, script_typing, store
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

    # Google account pin: preserved across edits; the optional
    # ``google_account`` arg re-pins (validated against the configured
    # accounts, stored as the durable email).
    prior_meta = store.read_meta(name)
    google_pin = prior_meta.extra.get("google_account")
    repin = (args.get("google_account") or "").strip() or None
    if repin:
        from app.tools.server.scripts.define_script import resolve_google_pin

        google_pin, pin_err = resolve_google_pin(repin)
        if pin_err:
            return {"ok": False, "error": pin_err}

    # Kind: preserved unless explicitly re-declared. An explicit
    # re-declare is ALSO the only sanctioned effects reset — sticky
    # union otherwise (a conditional writer must stay gated even after
    # an edit whose smoke didn't reach the write path).
    redeclared_kind: str | None = (args.get("kind") or "").strip() or None
    if redeclared_kind and redeclared_kind not in script_typing.KINDS:
        return {
            "ok": False,
            "error": f"`kind` must be one of {list(script_typing.KINDS)}",
        }

    result = await sandbox.smoke_test_async(name, candidate, google_account=google_pin)
    if not result.ok:
        return {"ok": False, "error": result.error, "traceback": result.traceback}

    meta = store.write_script(name, candidate)
    smoke_effects = script_typing.observed_effects(result.result, result.ctx)
    if redeclared_kind:
        kind = redeclared_kind
        effects = smoke_effects                      # explicit re-declare: reset
    else:
        kind = prior_meta.kind or script_typing.infer_kind(
            result.result, len(result.ctx.inline) if result.ctx else 0
        )
        effects = script_typing.merge_effects(prior_meta.effects, smoke_effects)
    patch: dict[str, Any] = {"kind": kind, "effects": effects}
    if repin and google_pin:
        patch["google_account"] = google_pin
    meta = store.update_meta(name, **patch)
    script_typing.mark_recently_edited(_ctx.session_id, name)
    return {
        "ok": True,
        "name": meta.name,
        "updated_at": meta.updated_at,
        "edits_applied": len(edits),
        "kind": kind,
        "effects": effects,
        "google_account": google_pin,
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
                "google_account": {
                    "type": "string",
                    "description": (
                        "Re-pin ctx.sheets to this Google account (email "
                        "or label). Omit to keep the script's existing pin."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["report", "chat", "job"],
                    "description": (
                        "Re-declare the script type. Omit to keep the "
                        "existing declaration. NOTE: an explicit re-declare "
                        "also resets the recorded effects (the confirm gate) "
                        "to this edit's smoke observation — use it after "
                        "removing a Sheets write to lift the gate."
                    ),
                },
            },
            "required": ["name", "edits"],
            "additionalProperties": False,
        },
        side="server",
        handler=_handler,
    )
)
