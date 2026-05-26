"""ScriptContext — minimal.

The ``ctx`` argument to ``build(ctx)``. Provides inputs + an inline
emission channel back to the chat + theme tokens + data access.
That's it. The LLM writes whatever HTML it wants and returns it
as a string; nothing else lives here.

  • Inline emitters (``text`` / ``image`` / ``json`` / ``log``) —
    surface Markdown / images / debug lines into the chat alongside
    the rendered report.
  • ``theme()`` — raw CSS-variables dict for the active plugin
    (host-detected). Keys are ``--voitta-*`` names from the
    plugin's ``static/theme.css``.
  • Data access (``snapshot`` / ``dataframe`` / ``raw`` /
    ``ensure_local``) — read python_storage caches + resolve
    canonical ``scheme://...`` refs.
  • ``args`` / ``host`` — script inputs (``host`` = the page the
    bookmarklet is mounted on, used for plugin-palette resolution).

Removed entirely (not coming back; the LLM does these directly in
the HTML it returns):
  ``three_scene``, ``apply_theme``, ``get_theme``, ``theme_css``,
  ``add_js``, ``add_css``, ``set_design``, ``set_template_theme``,
  ``fill_cards``, ``add_widget_stylesheets``.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class InlineItem:
    kind: str   # "text" | "image" | "json"
    payload: dict[str, Any]


@dataclass
class ScriptContext:
    slug: str
    args: dict[str, Any] = field(default_factory=dict)
    inline: list[InlineItem] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    # The page host the bookmarklet is mounted on. ``theme()`` uses
    # it to pick the matching plugin's palette.
    host: str | None = None
    # Canonical upstream-artefact refs resolved during this run.
    _resolved_refs: list[str] = field(default_factory=list)
    # Running event loop — set by sandbox.run() before offloading _execute
    # to a thread pool. Passed through to ensure_local so it can bridge
    # async resolvers back to the loop via run_coroutine_threadsafe.
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    # ---- inline emitters -------------------------------------------

    def text(self, body: str) -> None:
        """Emit a Markdown text block into the current chat turn."""
        if not isinstance(body, str):
            raise TypeError(f"ctx.text expects str, got {type(body).__name__}")
        self.inline.append(InlineItem("text", {"body": body}))

    def image(self, data: bytes | str, mime: str = "image/png", alt: str = "") -> None:
        """Emit a raw image. ``data`` is base64 str OR raw bytes."""
        if isinstance(data, (bytes, bytearray)):
            import base64
            data = base64.b64encode(bytes(data)).decode("ascii")
        if not isinstance(data, str):
            raise TypeError("ctx.image data must be bytes or base64 str")
        self.inline.append(
            InlineItem("image", {"data": data, "mime": mime, "alt": alt})
        )

    def json(self, value: Any) -> None:
        """Emit an arbitrary JSON-serialisable value into the chat."""
        self.inline.append(InlineItem("json", {"value": value}))

    def log(self, *parts: Any) -> None:
        """Append a debug line — surfaced via the tool result."""
        line = " ".join(str(p) for p in parts)
        self.log_lines.append(line)
        logger.debug("script[%s] %s", self.slug, line)

    # ---- theme -----------------------------------------------------

    def theme(self) -> dict[str, str]:
        """Return the raw CSS-variables dict for the active plugin.

        Keys are CSS-variable names (``--voitta-bg``, ``--voitta-text``,
        ``--voitta-accent``, ``--voitta-flow-edge-success``, etc.).
        Values are CSS color/token strings. Read via .get(...) with
        sensible fallbacks — the plugin determines which keys exist.

        Substitute into your CSS directly::

            t = ctx.theme()
            html = f'''<style>
              :root {{
                {"".join(f"{k}: {v};" for k, v in t.items())}
              }}
              body {{ background: var(--voitta-bg); }}
            </style>'''
        """
        if self.host:
            try:
                from app.tools.domain.theme import resolve_theme
                rich = resolve_theme(self.host)
                if rich.get("ok"):
                    raw = rich.get("raw_tokens") or {}
                    if isinstance(raw, dict):
                        return {
                            k: v for k, v in raw.items()
                            if isinstance(k, str) and isinstance(v, str)
                        }
            except Exception:
                pass
        # No host — return whatever the default plugin's :root tokens are
        # by reading the file directly. Returns an empty dict if it can't.
        try:
            from app.tools.domain.theme import resolve_theme
            rich = resolve_theme(None)
            if rich.get("ok"):
                raw = rich.get("raw_tokens") or {}
                if isinstance(raw, dict):
                    return {
                        k: v for k, v in raw.items()
                        if isinstance(k, str) and isinstance(v, str)
                    }
        except Exception:
            pass
        return {}

    # ---- data access ------------------------------------------------

    def snapshot(self, handle: str) -> dict:
        """Look up a python_storage snapshot record by handle."""
        from app.services import python_storage
        rec = python_storage.get(handle)
        if rec is None:
            raise KeyError(f"no python_storage snapshot {handle!r}")
        return rec

    def file(self, handle: str, filename: str | None = None) -> Path:
        """Return the Path to a file inside a python_storage snapshot.

        If *filename* is omitted, returns the first non-meta file in the
        snapshot directory (useful when the snapshot contains exactly one
        file, e.g. a veed_frame JPEG).
        """
        rec = self.snapshot(handle)
        snap_dir = Path(rec["path"])
        if filename:
            return snap_dir / filename
        # find first non-meta file
        skip = {"meta.json", "raw.json", "curves.pkl"}
        for f in sorted(snap_dir.iterdir()):
            if f.name not in skip and f.is_file():
                return f
        raise FileNotFoundError(f"snapshot {handle!r} contains no data files")

    def dataframe(self, handle: str):
        """Load ``curves.pkl`` for a curves-kind snapshot as a DataFrame."""
        import pandas as pd
        rec = self.snapshot(handle)
        pkl = Path(rec["path"]) / "curves.pkl"
        if not pkl.exists():
            raise FileNotFoundError(
                f"snapshot {handle!r} has no curves.pkl (kind not 'curves'?)"
            )
        return pd.read_pickle(pkl)

    def raw(self, handle: str) -> Any:
        """Read ``raw.json`` for a raw-kind snapshot."""
        rec = self.snapshot(handle)
        path = Path(rec["path"]) / "raw.json"
        if not path.exists():
            raise FileNotFoundError(f"snapshot {handle!r} has no raw.json")
        return _json.loads(path.read_text())

    def ensure_local(self, ref: str) -> str:
        """Materialise a ``scheme://...`` upstream-artefact ref to a local path."""
        from app.services.ensure_local import ensure_local as _ensure_local
        from app.services.refs import canonicalise, RefError
        try:
            canonical = canonicalise(ref)
            if canonical not in self._resolved_refs:
                self._resolved_refs.append(canonical)
        except RefError:
            pass
        return _ensure_local(ref, loop=self._loop)
