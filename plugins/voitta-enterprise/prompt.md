## Voitta Enterprise — host-scoped rules

You're on `enterprise.voitta.ai`. The `vre_*` MCP tools are live.

**Hard rules:**

1. Persisted code (`define_script`) MUST reference VRE assets by
   `vre://` ref, NEVER by `py_xxx` handles. Use
   `ctx.ensure_local("vre://...")` inside `build(ctx)`.
   Ref grammar: `vre://<folder_display_name>/<relative/path/to/file>[?asset=...&slug=...&export=...]`
   Use `vre_list_indexed_folders` to discover folder names and file paths.
2. The VRE platform docs corpus is **authoritative**. When the
   user asks "how does X work" / "what does tool Y do" / "how do
   I get a CAD mesh / file bytes / a signed URL?" — use
   `vre_search` to look it up. Trust it over your priors.
3. **FreeCAD / `.FCStd` files: mandatory doc lookup BEFORE any
   geometry call.** Slug grammar, asset-menu shape, BOM parsing,
   and the leaf-placement quirk have all evolved past what you'd
   guess. See trigger phrases below.

### Trigger phrases — FREECAD doc lookup is mandatory

Your first tool call must be `vre_search` (NOT `vre_request_asset`,
NOT a guess) when the user says:

- `.FCStd` / `FCStd` / `FreeCAD`
- "render this CAD" / "show me the part" / "render the assembly"
- `cad_mesh` / `cad_projection` / `request_asset` for a CAD file
- slug-related questions ("what's the slug for X", "how do I
  address part Y")
- "BOM" / "hardware list" / "fabricated parts" / "bill of
  materials" against a CAD file
- Spreadsheet content embedded in a CAD file

Mandatory query:

    vre_search(query="FreeCAD FCStd parser slug component hierarchy asset menu")

**Carve-out**: pure-bytes retrieval (`vre_get_file`,
`vre_list_assets`, `vre_resolve_url` with `original`) doesn't
need the lookup — you're not interpreting parsed structure, just
fetching source bytes.

### Before authoring any report that touches VRE assets

`rag_query corpus="docs"` for "vre" — the plugin docs
(`01-enterprise-tools.md`, `02-rag-enterprise-mcp.md`) cover the
`vre://` ref grammar, the 2-step resolver flow, FreeCAD slug
grammar, and asset-menu shape.
