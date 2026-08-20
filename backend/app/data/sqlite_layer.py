"""SQLite-backed Chainlit data layer.

No-auth single-user variant: all threads are stored and retrieved without
requiring an authenticated user. The schema creates a hardcoded "local"
user row that all threads reference so the sidebar thread list works.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.types import Feedback, PaginatedResponse, Pagination, ThreadFilter

logger = logging.getLogger(__name__)

_LOCAL_USER_ID = "00000000-0000-0000-0000-000000000001"
_LOCAL_USER_IDENTIFIER = "local"


def _auth_enabled() -> bool:
    """True in server mode (Chainlit native auth on). When True, Chainlit
    supplies the real authenticated user id on every thread read/write, so
    the local-user fallbacks below must NOT fire (they would defeat per-user
    isolation by collapsing everyone onto the single 'local' user)."""
    from chainlit.auth import require_login

    return require_login()

_SCHEMA_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        "id"         TEXT PRIMARY KEY,
        "identifier" TEXT NOT NULL UNIQUE,
        "metadata"   TEXT NOT NULL DEFAULT '{}',
        "createdAt"  TEXT
    )
    """,
    f"""
    INSERT OR IGNORE INTO users ("id", "identifier", "metadata", "createdAt")
    VALUES ('{_LOCAL_USER_ID}', '{_LOCAL_USER_IDENTIFIER}', '{{}}', datetime('now'))
    """,
    """
    CREATE TABLE IF NOT EXISTS threads (
        "id"             TEXT PRIMARY KEY,
        "createdAt"      TEXT,
        "name"           TEXT,
        "userId"         TEXT,
        "userIdentifier" TEXT,
        "tags"           TEXT,
        "metadata"       TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS steps (
        "id"              TEXT PRIMARY KEY,
        "name"            TEXT NOT NULL,
        "type"            TEXT NOT NULL,
        "threadId"        TEXT NOT NULL,
        "parentId"        TEXT,
        "disableFeedback" INTEGER NOT NULL DEFAULT 0,
        "streaming"       INTEGER NOT NULL DEFAULT 0,
        "waitForAnswer"   INTEGER,
        "isError"         INTEGER,
        "metadata"        TEXT,
        "tags"            TEXT,
        "input"           TEXT,
        "output"          TEXT,
        "createdAt"       TEXT,
        "start"           TEXT,
        "end"             TEXT,
        "generation"      TEXT,
        "showInput"       TEXT,
        "language"        TEXT,
        "indent"          INTEGER,
        "defaultOpen"     INTEGER NOT NULL DEFAULT 0,
        "autoCollapse"    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS elements (
        "id"          TEXT PRIMARY KEY,
        "threadId"    TEXT,
        "type"        TEXT,
        "url"         TEXT,
        "chainlitKey" TEXT,
        "name"        TEXT NOT NULL,
        "display"     TEXT,
        "objectKey"   TEXT,
        "size"        TEXT,
        "page"        INTEGER,
        "language"    TEXT,
        "forId"       TEXT,
        "mime"        TEXT,
        "props"       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedbacks (
        "id"       TEXT PRIMARY KEY,
        "forId"    TEXT NOT NULL,
        "threadId" TEXT NOT NULL,
        "value"    INTEGER NOT NULL,
        "comment"  TEXT
    )
    """,
]

# Columns added by newer chainlit versions. CREATE TABLE IF NOT EXISTS
# does nothing for pre-existing DBs, so each (table, column, ddl) here is
# retro-fitted with ALTER TABLE at startup. Without this, every step
# insert/update fails (and retries once per second from the status
# ticker), flooding the log — seen live when chainlit 2.11.1 started
# writing "defaultOpen" into a DB created before that column existed.
_MIGRATIONS = [
    ("steps", "defaultOpen", 'ALTER TABLE steps ADD COLUMN "defaultOpen" INTEGER NOT NULL DEFAULT 0'),
    ("steps", "autoCollapse", 'ALTER TABLE steps ADD COLUMN "autoCollapse" INTEGER NOT NULL DEFAULT 0'),
]


def _loads(raw: Any, default: Any) -> Any:
    if not isinstance(raw, str):
        return raw if raw is not None else default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


class SQLiteDataLayer(SQLAlchemyDataLayer):
    """SQLAlchemyDataLayer backed by a local SQLite file, no auth required."""

    def __init__(
        self,
        db_path: str,
        storage_provider: Optional[BaseStorageClient] = None,
        show_logger: bool = False,
    ) -> None:
        super().__init__(
            conninfo=f"sqlite+aiosqlite:///{db_path}",
            storage_provider=storage_provider,
            show_logger=show_logger,
        )
        self._schema_initialised = False
        self._local_user_id = _LOCAL_USER_ID  # resolved after ensure_schema

    # ── Schema bootstrap ──────────────────────────────────────────────────

    async def ensure_schema(self) -> None:
        """Create all tables and seed the local user row. Idempotent."""
        if self._schema_initialised:
            return
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            for stmt in _SCHEMA_STMTS:
                await conn.execute(text(stmt))
            for table, column, ddl in _MIGRATIONS:
                cols = await conn.execute(text(f"PRAGMA table_info({table})"))
                if column not in {r[1] for r in cols.fetchall()}:
                    await conn.execute(text(ddl))
                    logger.info("SQLiteDataLayer: migrated %s.%s", table, column)
            # The INSERT OR IGNORE above is skipped when a row with
            # identifier='local' already exists (e.g. from an older build
            # that used real auth).  Read back whoever actually owns the
            # 'local' identifier so _local_user_id is always valid.
            row = await conn.execute(
                text("SELECT id FROM users WHERE identifier = :i"),
                {"i": _LOCAL_USER_IDENTIFIER},
            )
            result = row.fetchone()
            if result:
                self._local_user_id = result[0]

        self._schema_initialised = True
        logger.info(
            "SQLiteDataLayer: schema ready at %s (local user id=%s)",
            self.engine.url,
            self._local_user_id,
        )

    # ── Write path ────────────────────────────────────────────────────────

    async def execute_sql(self, query: str, parameters: dict) -> Any:
        await self.ensure_schema()
        serialized = {
            k: json.dumps(v) if isinstance(v, list) else v
            for k, v in parameters.items()
        }
        return await super().execute_sql(query, serialized)

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        """Upsert thread. In no-auth (desktop) mode, link to the hardcoded
        local user when none is given. In server mode Chainlit passes the
        authenticated user's id, so we leave it untouched for native
        per-user ownership.

        Project scoping: every metadata upsert stamps the ACTIVE project
        slug (once — an existing tag is never overwritten, so a thread
        stays in the project it was born in even if the user switches
        mid-conversation). Threads with no tag predate projects and are
        treated as legacy by list_threads."""
        await self.ensure_schema()
        if user_id is None and not _auth_enabled():
            user_id = self._local_user_id
        if metadata is not None and "project" not in metadata:
            try:
                existing = await self.get_thread(thread_id=thread_id)
                prior = (existing or {}).get("metadata") or {}
                prior = _loads(prior, {}) if isinstance(prior, str) else prior
            except Exception:
                prior = {}
            if isinstance(prior, dict) and prior.get("project"):
                metadata = {**metadata, "project": prior["project"]}
            else:
                from app.services.projects import active_project

                metadata = {**metadata, "project": active_project()}
        await super().update_thread(
            thread_id=thread_id,
            name=name,
            user_id=user_id,
            metadata=metadata,
            tags=tags,
        )

    # ── Read path ─────────────────────────────────────────────────────────

    async def get_all_user_threads(
        self,
        user_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        await self.ensure_schema()
        # No-auth (desktop): default to the local user when nothing specified.
        # Server mode: Chainlit passes the authenticated user's id, so a None
        # here means "all" only in desktop — leave it for Chainlit otherwise.
        if user_id is None and thread_id is None and not _auth_enabled():
            user_id = self._local_user_id
        threads = await super().get_all_user_threads(
            user_id=user_id, thread_id=thread_id
        )
        if not threads:
            return threads
        for thread in threads:
            thread["tags"] = _loads(thread.get("tags"), [])
            thread["metadata"] = _loads(thread.get("metadata"), {})
            for step in thread.get("steps") or []:
                step["metadata"] = _loads(step.get("metadata"), {})
                step["generation"] = _loads(step.get("generation"), {})
                step["tags"] = _loads(step.get("tags"), [])
        return threads

    async def list_threads(
        self,
        pagination: Pagination,
        filters: ThreadFilter,
    ) -> PaginatedResponse:
        """List threads. No-auth (desktop): scope to the local user. Server
        mode: Chainlit's native route already sets filters.userId to the
        authenticated user, so we don't override it.

        Project scoping: only the ACTIVE project's threads are returned —
        a thread's ``metadata.project`` tag must match (untagged threads
        predate projects and belong to legacy). Post-filter in Python:
        metadata is a JSON string column, and thread lists are small at
        this product's scale."""
        await self.ensure_schema()
        if not filters.userId and not _auth_enabled():
            filters.userId = self._local_user_id
        resp = await super().list_threads(pagination, filters)
        try:
            from app.services.projects import LEGACY_SLUG, active_project

            active = active_project()
            kept = []
            for t in resp.data:
                md = t.get("metadata") if isinstance(t, dict) else None
                md = _loads(md, {}) if isinstance(md, str) else (md or {})
                slug = md.get("project") or LEGACY_SLUG
                if slug == active:
                    kept.append(t)
            resp.data = kept
        except Exception:
            pass  # a filter failure must never hide the thread list entirely
        return resp
