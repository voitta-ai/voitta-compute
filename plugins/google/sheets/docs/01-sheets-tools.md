# Google Sheets tools

Nine LLM-facing tools backed by the Sheets API v4. All except
`sheets_get_page_context` require an active Google OAuth connection with
the `spreadsheets` scope (connect via the Google Drive settings panel).

## From scripts

Backend scripts have full access to all Sheets operations via `ctx.sheets`.
See [02-ctx-sheets.md](02-ctx-sheets.md) for the full API reference and
[03-ctx-sheets-examples.md](03-ctx-sheets-examples.md) for worked examples.
Pass `spreadsheet_id` to `run_script` via the `args` field so scripts can
use it as `ctx.args["spreadsheet_id"]`.

## sheets_get_page_context

Parses the current `docs.google.com` URL to extract:

- `spreadsheet_id` — the ID component from the URL (required by all other tools)
- `sheet_name` — the active sheet name (from `#gid=` if resolvable via metadata, else null)
- `gid` — the numeric sheet GID from the URL fragment
- `title` — the page title

No OAuth required. Always call this first.

## sheets_get_metadata

`GET /v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties`

Returns all sheets in the workbook: name, GID, row count, column count.
Use this to confirm sheet names before constructing range strings.

## sheets_read_range

`GET /v4/spreadsheets/{spreadsheet_id}/values/{range}`

Parameters:
- `spreadsheet_id` — from `sheets_get_page_context`
- `range` — A1 notation, e.g. `Sheet1!A1:D20` or just `A1:D20` for the first sheet
- `value_render` — `"FORMATTED_VALUE"` (default) | `"UNFORMATTED_VALUE"` | `"FORMULA"`

Returns `{range, values: [[...], ...], row_count, col_count}`.

## sheets_write_range

`PUT /v4/spreadsheets/{spreadsheet_id}/values/{range}`

Parameters:
- `spreadsheet_id`, `range` — same as above
- `values` — list of lists (rows × columns)
- `value_input_mode` — `"USER_ENTERED"` (default, interprets formulas) | `"RAW"`

Returns `{updated_range, updated_rows, updated_columns, updated_cells}`.

**Always confirm with the user before calling this tool.**

## sheets_append_rows

`POST /v4/spreadsheets/{spreadsheet_id}/values/{range}:append`

Appends rows after the last non-empty row in the table anchored at `range`.

Parameters:
- `spreadsheet_id`, `range` — table anchor (e.g. `Sheet1!A1`)
- `values` — rows to append
- `value_input_mode` — same as write_range

Returns `{updates: {updated_range, updated_rows, updated_cells}}`.

**Always confirm with the user before calling this tool.**
