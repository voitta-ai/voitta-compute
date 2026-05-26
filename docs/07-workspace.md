# Workspace — Data, Scripts, and Folders

The **Workspace** is the persistent storage layer visible in the right-hand
pane's Workspace tab. It holds two kinds of artefact:

- **Data snapshots** — files written by tools (images, JSON, CSVs, frames…),
  stored in `python_storage/cache/`.
- **Scripts** — user-authored Python report scripts stored in `scripts/`.

Both can be organised into **folders**.

---

## Filesystem layout

```
python_storage/cache/
  snapshot_{handle}/          ← unfoldered snapshot
    meta.json                 ← handle, kind, label, created_at, files[], origin{}
    <file>                    ← the actual data file(s)
  folders/
    {folder_name}/
      folder.json             ← name, description, color, created_at
      snapshot_{handle}/      ← foldered snapshot (same structure)

scripts/
  {slug}/                     ← unfoldered script
    code.py
    meta.json
  folders/
    {folder_name}/
      folder.json             ← name, description, color, created_at
      {slug}/
        code.py
        meta.json
  scripts_state/              ← render state (always flat, never moves)
    errors/{slug}.jsonl
    inventory/{slug}.json
```

The `scripts_state/` directory is keyed by slug only and **never moves**
into a folder — it's ephemeral render state, not source.

---

## Folder naming rules

Folder names must match `[a-z0-9_-]`, max 64 characters. Examples:
`veed-frames`, `client_reports`, `drafts`.

---

## LLM tools

| Tool | What it does |
|---|---|
| `create_folder(name, kind?, description?)` | Create a folder. `kind`: `"data"`, `"scripts"`, or `"both"` (default). No-op if already exists. |
| `move_to_folder(id, folder_name?)` | Move a snapshot (handle) or script (slug) into a folder. `folder_name=null` moves back to root. |
| `delete_folder(name)` | Delete a folder. **Contents are moved to root, not deleted.** |
| `list_data()` | Lists all snapshots; each entry includes `folder_name`. |
| `list_scripts()` | Lists all scripts; each entry includes `folder_name`. |

Both `put_file()` (called internally by tools like `veed_frame`) and
`define_script()` accept an optional `folder_name` param so artefacts
land directly in the right folder without a separate move.

---

## Best practices

### Every artefact goes into a folder — no exceptions

Folders are filesystem-native — OS-visible, no magic, self-describing.
Root-level items accumulate fast and become unnavigable. Default to a
folder for every script and every data snapshot, even if there is only
one item so far.

Infer a name from context: video title, project name, client, topic.
`create_folder` is a no-op if the folder already exists, so calling it
defensively before every write costs nothing.

```
veed-frames/        ← all frame extracts for a project
client-acme/        ← scripts + data for a specific client
drafts/             ← work-in-progress scripts
```

### Always provide a description for data snapshots

When tools like `veed_frame` store a file, they write a `label` into
`meta.json`. This label is what appears in the Workspace panel and in
`list_data()`. A good label makes it immediately obvious what the file
is without opening it:

- Good: `"intro-clip — first frame"`, `"Q3 revenue export — Drive"`
- Bad: `"py_71e1c5b2"`, `"file"`

If you're storing something programmatically, pass a descriptive `label`
in the `meta` dict to `put_file()`.

### Keep the workspace clean — delete what you no longer need

`delete_folder(name)` orphans contents to root (safe). Then delete
individual snapshots with the trash icon in the Workspace panel or by
calling the workspace API. Don't accumulate hundreds of unnamed frames.

### Folder before creating

If you're about to produce several related artefacts (e.g. multiple
frames from a video, or a suite of reports for one project), create the
folder first, then produce the artefacts into it. Avoids a move step.

```
# Good sequence:
create_folder("acme-analysis", description="ACME project frames and reports")
veed_frame(clip_uuid=..., folder_name="acme-analysis")
define_script("acme-revenue", code=..., folder_name="acme-analysis")
```

### `list_data` before writing a report that reads data

Always call `list_data()` before writing a report that references
python_storage handles. The handle `py_xxxxxxxx` is opaque — you need
to see the current `name`, `kind`, `files[]`, and `folder_name` to
know what's actually there. Handles from earlier in the conversation
may have been deleted.

---

## Reading files from storage in reports

Three patterns, in order of preference:

**1. `ctx.file(handle)` — simplest:**
```python
def build(ctx):
    path = ctx.file("py_71e1c5b2")       # first non-meta file in snapshot
    import base64
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f'<img src="data:image/jpeg;base64,{b64}">'
```

**2. `ctx.ensure_local("py://handle/filename")` — URI style:**
```python
def build(ctx):
    path = ctx.ensure_local("py://py_71e1c5b2/frame_0.00s.jpg")
    ...
```

**3. `ctx.snapshot(handle)` — raw record:**
```python
def build(ctx):
    rec = ctx.snapshot("py_71e1c5b2")   # {"handle", "path", "meta", "folder_name"}
    snap_dir = __import__("pathlib").Path(rec["path"])
    ...
```

The `ctx.file()` approach is preferred unless you need `meta.json`
contents. Both approaches work regardless of whether the snapshot is
at root or inside a folder — the path in `rec["path"]` is always the
absolute resolved directory.

---

## Workspace UI

The Workspace tab in the report pane shows:

- **Folders** (collapsed by default) — each shows its name,
  description, item count, and a delete button. Expand to see the
  scripts and data inside.
- **Scripts** (unfoldered) — run in-app (▶), open in new tab (⧉),
  delete (🗑). Move button appears when folders exist.
- **Data** (unfoldered) — expand to see individual files with inline
  image thumbnails. Eye button previews images/video/audio/text.
  Move button appears when folders exist.

The **＋ New folder** button (header) opens an inline name field.
Folder name is auto-sanitised to `[a-z0-9_-]` as you type.

The **move button** (↑ folder icon) on each item opens a dropdown
listing available folders plus "Remove from folder".
