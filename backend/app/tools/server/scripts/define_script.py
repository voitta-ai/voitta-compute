"""``define_script(name, code)`` — author a new script.

Smoke-tests ``build(ctx)`` before persisting. On smoke-test failure no
files land on disk; the traceback is returned so the LLM can fix and
retry without leaking half-written state.

Google account pin: which Google account ``ctx.sheets`` acts as is
decided when the script is SAVED — either the explicit
``google_account`` arg or, absent that, the settings-default account's
email at save time — and stamped into the script's meta. Re-runs are
thereby reproducible: the script uses the same account no matter what
the default is changed to later.
"""

from __future__ import annotations

from typing import Any

from app.reports import sandbox, store
from app.reports.slug import InvalidSlug, validate_slug
from app.services import google_oauth
from app.tools.registry import ToolCtx, ToolSpec, registry


def resolve_google_pin(selector: str | None) -> tuple[str | None, str | None]:
    """Map an explicit selector (or None → current default account) to
    the account EMAIL to pin. Returns ``(pin, error)``.

    Explicit selector that doesn't resolve, or that resolves to a
    not-yet-connected account (no email — nothing durable to pin), is an
    error. No selector + no connected default just means "no pin"."""
    if selector:
        try:
            account_id = google_oauth.resolve_account(selector)
        except google_oauth.UnknownAccount as exc:
            return None, str(exc)
        email = google_oauth.account_email(account_id)
        if not email:
            return None, (
                f"Google account {selector!r} is not connected yet — "
                "connect it in Settings → Google before pinning a script "
                "to it."
            )
        return email, None
    try:
        account_id = google_oauth.resolve_account(None)
    except google_oauth.UnknownAccount:
        return None, None  # no accounts configured — script gets no pin
    return google_oauth.account_email(account_id), None


async def _handler(args: dict[str, Any], _ctx: ToolCtx) -> dict[str, Any]:
    name = args.get("name") or ""
    code = args.get("code") or ""
    folder_name: str | None = args.get("folder_name") or None
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "`code` must be a non-empty string"}
    try:
        validate_slug(name)
    except InvalidSlug as exc:
        return {"ok": False, "error": str(exc)}
    google_pin, pin_err = resolve_google_pin(
        (args.get("google_account") or "").strip() or None
    )
    if pin_err:
        return {"ok": False, "error": pin_err}
    if store.exists(name):
        return {
            "ok": False,
            "error": (
                f"script {name!r} already exists — use edit_script to "
                "change it. Do NOT delete_script + define_script: that "
                "loses history and wastes a turn re-sending the full "
                "source. Even a full rewrite is one edit_script call "
                "(find = the entire old source, replace = the new one); "
                "read the current source with get_script first."
            ),
        }
    result = await sandbox.smoke_test_async(name, code, google_account=google_pin)
    if not result.ok:
        return {"ok": False, "error": result.error, "traceback": result.traceback}
    try:
        meta = store.write_script(name, code, folder_name=folder_name)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "hint": (
                f"Retry define_script — if the folder {folder_name!r} "
                "still can't be created, call create_folder first."
            ) if folder_name else None,
        }
    if google_pin:
        meta = store.update_meta(name, google_account=google_pin)
    return {
        "ok": True,
        "name": meta.name,
        "folder_name": meta.folder_name,
        "created_at": meta.created_at,
        "google_account": google_pin,
        "smoke": {"log_lines": result.ctx.log_lines if result.ctx else []},
    }


registry.register(
    ToolSpec(
        name="define_script",
        description=(
            "Create a NEW script under scripts/<name>/code.py. The script "
            "must define `build(ctx)`. Returns ok=true only if `build` "
            "executes cleanly during a smoke-test; otherwise nothing is "
            "written and the traceback is returned. Fails if the name "
            "already exists — run list_scripts BEFORE composing code, and "
            "use edit_script (never delete_script + define_script) to "
            "change an existing script."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "slug: lowercase letters, digits, underscore, hyphen (1..64)",
                },
                "code": {
                    "type": "string",
                    "description": "full Python source of the script",
                },
                "folder_name": {
                    "type": "string",
                    "description": "Workspace folder to place the script in. Auto-created if it doesn't exist.",
                },
                "google_account": {
                    "type": "string",
                    "description": (
                        "Google account (email or label) ctx.sheets should "
                        "act as, pinned into the script's meta. Omit to pin "
                        "the current default account."
                    ),
                },
            },
            "required": ["name", "code"],
            "additionalProperties": False,

        },
        side="server",
        handler=_handler,
    )
)
