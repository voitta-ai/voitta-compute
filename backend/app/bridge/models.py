"""Wire types for the `/tools/*` endpoints.

Names match `docs/05-bridge-protocol.md`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PageInfo(BaseModel):
    href: str | None = None
    host: str | None = None
    origin: str | None = None
    pathname: str | None = None
    search: str | None = None
    hash: str | None = None
    title: str | None = None
    referrer: str | None = None
    loaded_at: str | None = None


class Viewport(BaseModel):
    w: int | None = None
    h: int | None = None
    dpr: float | None = None


class RegisterRequest(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=128)
    capabilities: list[str] = Field(default_factory=list)
    page: PageInfo | None = None
    user_agent: str | None = None
    viewport: Viewport | None = None
    release_tag: str | None = None


class RegisterResponse(BaseModel):
    session_id: str
    tools: list[str]


class ToolResultEnvelope(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=128)
    call_id: str = Field(..., min_length=4, max_length=64)
    ok: bool
    result: Any | None = None
    error: dict[str, Any] | None = None


class TestEchoRequest(BaseModel):
    session_id: str
    primitive: str
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = 10_000
