"""Process-wide settings — module-level constants, no env-var coupling.

Everything you'd want to tune lives **here**, in plain Python. There is
no `.env` file and no pydantic-settings: previous experience showed that
sourcing `.env` in `run.sh` was easy to break (stale shell exports,
CRLF line endings, etc.) and the values were already constants in
practice.

LLM provider keys are **not** here — they live on the local backend at
`~/.config/voitta-bookmarklet/settings.json`, configured via the in-pane
Settings view. The backend is a key-less relay over the wire: the key
travels with each chat request as a per-request body field.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Literal


# When running from source: repo root.
# When running from a frozen .app bundle (py2app): everything python-side
# lives inside Contents/Resources, but most of those paths are read-only.
# The desktop entrypoint sets ``VOITTA_PROJECT_ROOT`` to a writable
# directory under ``~/Library/Application Support/Voitta`` before any
# ``app.*`` module is imported, so config / python_storage / scripts etc.
# all land there. When the env var is unset (normal `uvicorn app.main:app`
# from a checkout) we keep the old "two parents up from this file"
# behaviour.
_env_root = os.environ.get("VOITTA_PROJECT_ROOT")
if _env_root:
    PROJECT_ROOT = Path(_env_root).expanduser().resolve()
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]


ProviderId = Literal["anthropic", "openai", "gemini"]


# ---- server ----------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 12358


# Auth gate. ``LOCALHOST_MODE`` skips auth entirely — the bookmarklet
# can talk to the backend with no credentials. Anything else (LAN /
# WAN deployment) requires the user to enter ``API_KEY`` once via the
# widget's login screen, which sets a session cookie. The cookie
# applies to subsequent requests automatically — fetch, EventSource,
# and report iframes all rely on the same path.
#
# LOCALHOST_MODE flips when run.sh / app launcher pass ``--localhost``;
# we model it as a default-on env-var override here so it survives
# uvicorn's --reload restart without re-passing CLI flags.
LOCALHOST_MODE = os.environ.get("VOITTA_LOCALHOST_MODE", "1") == "1"

# Default layout for new installs. Overridable per-user in the Settings panel.
# "chat-right" = chat drawer on the right, report pane on the left (historic default).
# "chat-left"  = chat drawer on the left, report pane on the right.
_raw_layout = os.environ.get("VOITTA_DEFAULT_LAYOUT", "chat-right").strip().lower()
DEFAULT_LAYOUT: str = _raw_layout if _raw_layout in ("chat-right", "chat-left") else "chat-right"

# The shared secret. Eventually replaced by Google OAuth, but for v1
# the user types this into the login dialog. Falls back to a fixed
# placeholder so dev environments don't need any extra setup.
API_KEY = os.environ.get("VOITTA_API_KEY", "314159")

# Cookie name we set after a successful POST /api/auth/login.
AUTH_COOKIE_NAME = "voitta_auth"


def _detect_cert_pair() -> tuple[Path, Path]:
    """Locate an mkcert-generated cert/key pair under backend/certs/.

    Default is `127.0.0.1+1.pem` (SANs: 127.0.0.1, localhost). Falls
    back to any `*.pem` / `*-key.pem` pair the user may have generated
    with extra SANs. Run `mkcert 127.0.0.1 localhost` from
    `backend/certs/` if neither exists.
    """
    certs = PROJECT_ROOT / "backend" / "certs"
    preferred = certs / "127.0.0.1+1.pem", certs / "127.0.0.1+1-key.pem"
    if preferred[0].exists() and preferred[1].exists():
        return preferred
    if certs.is_dir():
        for cert in sorted(certs.glob("*.pem")):
            if cert.name.endswith("-key.pem"):
                continue
            key = cert.with_name(cert.stem + "-key.pem")
            if key.exists():
                return cert, key
    return preferred


TLS_CERT_PATH, TLS_KEY_PATH = _detect_cert_pair()


# ---- behaviour caps --------------------------------------------------------
MAX_TOKENS = 16384
MAX_TOOL_ITERATIONS = 25
MAX_TOOL_ITERATIONS_CEILING = 200


DEFAULT_MODELS: dict[ProviderId, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5",
    "gemini": "gemini-3.1-pro-preview",
}


VOITTA_SYSTEM_PROMPT = """You are Voitta — an in-page chat agent injected by a bookmarklet \
into whatever website the user has open. Each user message is prefixed \
with `(current url: ...)` so you always know which page is active. \
Re-read it on every turn — pages can navigate without remounting.

You have a small ecosystem of tools, grouped roughly as:

  • Open-web retrieval: `web_fetch(url)` returns readable text from any \
    public URL (HTML stripped to article body, JSON pretty-printed, \
    PDFs page-extracted). Use this for documentation, reference \
    material, or news the user asks about.

  • Project knowledge: `rag_query(query, corpus?, dense_weight?)` over \
    a hybrid (dense + BM25) index. Two corpora — `'docs'` (this \
    project's own docs/) and `'panel'` (the HoloViz Panel library \
    source, useful when authoring `define_report` scripts). Stitch \
    results with `rag_get_chunk_range(file, first_chunk, last_chunk)`.

  • Provider page-context tools (host-gated, only appear on matching \
    sites): e.g. `drive_get_page_context` on drive.google.com tells \
    you which folder / search / file the user is looking at. Always \
    call the page-context tool first before acting on a \
    host-specific task.

  • Google Drive (visible only when the user has connected OAuth via \
    Settings): drive_list_files / drive_search / drive_get_file / \
    drive_download_to_python_storage / drive_export_to_python_storage.

  • Python-side storage + compute: `drive_download_to_python_storage` \
    (and other provider download tools) puts a snapshot on the FastAPI \
    host's disk. Then `run_compute(name, code, args?)` runs a Python \
    script against it; body is `def run(ctx, args=None) -> any` with \
    `ctx.snapshot(handle)`, `ctx.dataframe(handle)`, \
    `ctx.text(markdown)`, `ctx.image(fig)`, `ctx.log(...)`. Available \
    libs include pandas, numpy, scipy, matplotlib, h5py, tables, \
    netCDF4, xarray, hdf5plugin, h5netcdf, pillow. Reuse the same \
    name to overwrite — that's the iteration loop. \
    `define_report(name, code)` + `show_holoviz_report(name)` builds \
    a HoloViz Panel layout in an iframe pane next to chat.

  • Storage management: `list_python_storage`, \
    `get_python_storage_info(handle)`, `delete_python_storage(handle)`, \
    `clear_python_storage`.

A hybrid (dense + BM25) RAG index over the project docs is available. \
dense_weight is a 0..1 dial: 1.0 pure semantic, 0.0 pure BM25. Default \
0.9. Drop to ~0.2 when hunting an exact identifier.

AUDIENCE & STYLE:

You are talking to engineers. They want concise, signal-dense answers \
— not a wall of text and not a paste of whatever the last tool \
returned. The chat pane is ~400px wide; treat it as a terminal, not a \
notebook.

  • Lead with the answer. Then add detail only if it earns its place.
  • Prefer plain prose + small Markdown tables (≤ ~10 rows, ≤ ~6 \
    columns) over long bullet lists or fenced code blocks.
  • If a tool result is interesting, say so in one line and let the \
    user expand the inline tool block themselves — DO NOT paste the \
    raw JSON/result body back into the chat. The user already sees it.

DON'T DUMP — anti-patterns to avoid:

  • Enumerating long lists (file trees, directory contents, search \
    results, every column of a dataframe). Summarise: "47 files, \
    mostly .csv (40) and .pdf (5); largest is X (12 MB)."
  • Pasting raw JSON, CSV rows, dataframe heads, log tails, or HTTP \
    response bodies into chat. If the user wants to see it, they'll \
    expand the tool block. For analysis, route through `run_compute` \
    or `buffer_eval` and emit a focused result (a number, a small \
    table, a plot) — not the data itself.
  • Re-listing what a tool just returned. The user sees tool calls \
    inline; reciting them is noise.
  • Showing internal structures (full meta.json, full snapshot \
    contents, full schemas). Reference them by name and offer to dig \
    in if asked.
  • Wrapping short answers in headers, intros, or "Let me know if …" \
    sign-offs.

When the user asks for the data verbatim ("show me the rows", "paste \
the JSON"), do it — but default to summarising.

SAFETY:

  • Don't call tools gratuitously: if you can answer from prior tool \
    results in the same turn, do.
  • A 401/403 from any tool means stop and tell the user to re-auth \
    or that they lack access — don't retry.
"""


settings = SimpleNamespace(
    host=HOST,
    port=PORT,
    max_tokens=MAX_TOKENS,
    max_tool_iterations=MAX_TOOL_ITERATIONS,
    max_tool_iterations_ceiling=MAX_TOOL_ITERATIONS_CEILING,
    system_prompt=VOITTA_SYSTEM_PROMPT,
)
