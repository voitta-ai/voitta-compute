## Google Drive — host-scoped LLM rules

You are talking to the user on `drive.google.com`. The `drive_*` tools
are live. The rules below apply only on this host.

### The `drive://` ref scheme — reports reference upstream, not handles

Core rule (see `VOITTA_SYSTEM_PROMPT` § "REPORTS — REFERENCE UPSTREAM
ARTEFACTS"): reports never bake `py_xxx` handles into their source.
For anything sourced from Google Drive, the canonical ref is:

    drive://file_id=<FILE_ID>[&export=<mime>]

Keys (URL-encode values that contain `/` or `;`):

  • `file_id` — required. The Drive file id (the `1AbCdEf...` string,
    not the file name). Stable for the lifetime of the file.
  • `export` — only for Google-native types (Docs, Sheets, Slides).
    Picks an export format: `text/csv`, `application/pdf`,
    `text/plain`, etc. Omitted for normal binary files (.csv, .pdf,
    .xlsx already uploaded) — those go through plain download, not
    export. If you don't know whether a file is native or binary,
    `drive_get_file(file_id)` tells you (look for `is_google_native`).

Examples:

    drive://file_id=1aBcDeF...XYZ
    drive://file_id=1aBcDeF...XYZ&export=text/csv
    drive://file_id=1aBcDeF...XYZ&export=application/pdf

### How the Drive resolver works under the hood

The resolver hides two pieces of nuance:

  1. **Auth refresh.** OAuth tokens expire. The Drive download tools
     (`drive_download_to_python_storage`, `drive_export_to_python_storage`)
     already transparently refresh on 401 — the resolver just calls
     them, so the LLM never has to think about token freshness.

  2. **Native vs binary.** When `export` is set, the resolver calls
     `drive_export_to_python_storage(file_id, format=export)`.
     Otherwise it calls `drive_download_to_python_storage(file_id)`.
     The resulting snapshot's `meta.json::origin` records the canonical
     `drive://` ref so subsequent `ensure_local` calls hit the cache.

The right pattern at report-authoring time is:

    def build(ctx):
        csv_path = ctx.ensure_local(
            "drive://file_id=1AbCdEf...XYZ&export=text/csv"
        )
        df = pd.read_csv(csv_path)
        # ...

Not:

    def build(ctx):
        rec = ctx.snapshot("py_a1b2c3")      # ← brittle
        df = pd.read_csv(rec["path"] + "/" + rec["meta"]["stored_name"])

### When you actually need a handle

If you're doing a one-shot analysis (not authoring a report), going
through `drive_download_to_python_storage` and reading the resulting
handle is fine — the handle's lifetime matches the conversation. The
upstream-ref rule applies to *persisted* code (`define_report`,
`define_compute_*`), where someone else will run it later.
