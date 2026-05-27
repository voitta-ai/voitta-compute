## Google Drive — host-scoped rules

You're on `drive.google.com`. The `drive_*` tools are live.

**Hard rules:**

1. Persisted code (`define_script`) MUST reference Drive content
   by `drive://` ref, NEVER by `py_xxx` handles. Use
   `ctx.ensure_local("drive://file_id=...")` inside `build(ctx)`.
2. One-shot analysis (no script being saved) can use handles
   from `drive_download_to_python_storage` — they're conversation-
   scoped.

**Before authoring a Drive-touching report**, `rag_query
corpus="docs"` for "drive" — the plugin's `01-drive-tools.md`
covers the `drive://` ref grammar, native-vs-binary distinction,
and the `ensure_local` flow.
