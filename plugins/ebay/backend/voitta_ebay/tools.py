"""eBay tool registrations — every tool is a thin shim over a
browser primitive that does the actual DOM scraping.

The primitives (defined in plugins/ebay/frontend/widget.ts) refuse to
run on the wrong page and surface PrimitiveError envelopes; we let
those propagate through ``call_browser`` as ``BrowserToolError``
which the registry wraps into the standard tool-error envelope.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# Serialization mutex for tools that drive the inbox UI. eBay renders
# the open-message body in a SINGLE iframe — concurrent ebay_read_message
# calls click multiple cards in rapid succession and the iframe only
# ever shows the last one's content, while earlier polls observe
# "open: false" because their iframe state was destroyed mid-poll.
# A per-session mutex serialises clicks so each call sees a stable
# iframe lifecycle.
#
# Per-session keying lets parallel chats from different bookmarklet
# sessions still run independently; only same-session concurrency is
# blocked.
_ebay_click_mutex: dict[str, asyncio.Lock] = {}


def _click_lock(ctx: ToolCtx) -> asyncio.Lock:
    key = ctx.session_id or "default"
    lock = _ebay_click_mutex.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ebay_click_mutex[key] = lock
    return lock


# ---- ebay_get_page_context ------------------------------------------------


async def _get_page_context(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser("ebay_inspect_page", {}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="ebay_get_page_context",
        description=(
            "What page is the user looking at on ebay.com? Returns "
            "{url, page_type, title, search_query, item_id, "
            "seller_id_in_url, category_id_in_url, params}. "
            "page_type ∈ {home, search, item, seller, store, "
            "category, myebay, other}.\n"
            "\n"
            "Always call this first for any eBay-flavoured task — it "
            "tells you which downstream tool is appropriate (search → "
            "ebay_scrape_search; item → ebay_scrape_item)."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_get_page_context,
        side="hybrid",
    )
)


# ---- ebay_scrape_search ---------------------------------------------------


async def _scrape_search(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser(
            "ebay_scrape_search",
            {
                "limit": int(args.get("limit") or 50),
                "include_ads": bool(args.get("include_ads", False)),
            },
            ctx,
        )
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="ebay_scrape_search",
        description=(
            "Read the visible search results from a /sch/ page. Returns "
            "{search_query, total_dom_cards, returned, hits: [...]}. "
            "Each hit: {listing_id, title, price, href, image, subtitle}.\n"
            "\n"
            "Errors: {ok: false, error: 'wrong_page'} if the user isn't "
            "on a search page — ask them to navigate to a search, or "
            "use ebay_navigate to take them there.\n"
            "\n"
            "By default skips the 'Shop on eBay' placeholder ad eBay "
            "always inserts at the top of search results. Set "
            "include_ads=true to keep them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "include_ads": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        handler=_scrape_search,
        side="hybrid",
    )
)


# ---- ebay_scrape_item -----------------------------------------------------


async def _scrape_item(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser("ebay_scrape_item", {}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="ebay_scrape_item",
        description=(
            "Read the active /itm/<id> page. Pulls the schema.org "
            "Product JSON-LD (clean structured data: name, price, "
            "currency, condition, images, shipping, returns) plus DOM "
            "fallbacks for fields not in the JSON-LD (seller name, "
            "username from URL).\n"
            "\n"
            "Returns {item_id, has_jsonld_product, product_jsonld, "
            "breadcrumbs, seller, dom_*}.\n"
            "\n"
            "Errors: {ok: false, error: 'wrong_page'} if the user isn't "
            "on an item page — use ebay_navigate to send them to "
            "/itm/<id>."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_scrape_item,
        side="hybrid",
    )
)


# ---- ebay_scrape_myebay ---------------------------------------------------


async def _scrape_myebay(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser(
            "ebay_scrape_myebay",
            {"order_limit": int(args.get("order_limit") or 50)},
            ctx,
        )
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="ebay_scrape_myebay",
        description=(
            "Read the user's My eBay dashboard at "
            "/mye/myebay/summary. Returns:\n"
            "  • recently_viewed: [{title, price, shipping, href, image, item_id}]\n"
            "  • orders: [{status, order_date, order_total, "
            "order_number, details_url, listing_id, item_title, "
            "item_image}]\n"
            "\n"
            "Status values from eBay include 'Delivered', 'In transit', "
            "'Shipped', 'Order placed', etc. order_total is "
            "user-locale formatted ('US $37.27').\n"
            "\n"
            "Errors: {ok: false, error: 'wrong_page'} if the user "
            "isn't on a My eBay page — use ``ebay_navigate(url='"
            "/mye/myebay/summary')``."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "order_limit": {
                    "type": "integer", "minimum": 1, "maximum": 200, "default": 50,
                    "description": "Cap on the orders array length.",
                },
            },
            "additionalProperties": False,
        },
        handler=_scrape_myebay,
        side="hybrid",
    )
)


# ---- ebay_list_messages ---------------------------------------------------


async def _list_messages(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser(
            "ebay_list_messages",
            {"limit": int(args.get("limit") or 100)},
            ctx,
        )
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="ebay_list_messages",
        description=(
            "Read the inbox at /cnt/ViewMessage. Returns "
            "{folder, total_visible, returned, cards: "
            "[{conversation_id, sender, subject, date, unread, "
            "from_ebay, image}]}.\n"
            "\n"
            "Use ``conversation_id`` from a card to open + read its "
            "body via ``ebay_read_message``.\n"
            "\n"
            "Errors: {ok: false, error: 'wrong_page'} if the user "
            "isn't on the messages page — use "
            "``ebay_navigate(url='/cnt/ViewMessage')``."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 500, "default": 100,
                },
            },
            "additionalProperties": False,
        },
        handler=_list_messages,
        side="hybrid",
    )
)


# ---- ebay_read_message ----------------------------------------------------


async def _read_message(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    # Serialise: only one in-flight click+read at a time per session.
    # Concurrent calls would race the single iframe and produce
    # spurious "open: false" envelopes that look like failures.
    async with _click_lock(ctx):
        try:
            info = await call_browser(
                "ebay_read_message",
                {
                    "conversation_id": args.get("conversation_id") or "",
                    "max_text_chars": int(args.get("max_text_chars") or 8000),
                    "include_html": bool(args.get("include_html", False)),
                },
                ctx,
            )
        except BrowserToolError as exc:
            return {"ok": False, "error": exc.kind, "message": str(exc)}

        # Surface "iframe never loaded" as a hard failure rather than
        # a soft ``open: false`` so the LLM doesn't retry-flood. The
        # cause is almost always the card being out of viewport
        # (virtualised list) or eBay's React being slow on a stale
        # session — both warrant a single human-friendly message
        # rather than another auto-retry.
        if not info.get("open"):
            return {
                "ok": False,
                "error": "iframe_did_not_load",
                "message": (
                    "Card click fired but the message body iframe didn't populate. "
                    "Ask the user to scroll the inbox so the target card is in "
                    "view, then call this tool ONCE more — do not call it "
                    "repeatedly in parallel; concurrent calls clobber each "
                    "other's iframe state."
                ),
                **info,
            }
        return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="ebay_read_message",
        description=(
            "Read the body of an open eBay message. Without "
            "``conversation_id``, reads whichever message the user "
            "currently has open in the right pane. With one, clicks "
            "the matching card to open it (scrolls into view first, "
            "polls the iframe for up to 4s) then reads.\n"
            "\n"
            "**SERIAL CALLS ONLY.** eBay renders open messages in a "
            "single shared iframe — calling this tool concurrently "
            "across multiple conversation_ids races the iframe and "
            "every call returns ``error: 'iframe_did_not_load'``. "
            "To read N messages: call this tool, AWAIT, then call "
            "again. Do NOT fan out parallel calls.\n"
            "\n"
            "Returns "
            "{open, conversation_id, sender, subject, date, "
            "body_text, body_text_full_chars, body_html, iframe_error}.\n"
            "\n"
            "``body_text`` is the iframe's plain-text rendering, "
            "capped at ``max_text_chars`` (default 8000). Pass "
            "``include_html: true`` to also return the raw HTML, "
            "useful when you need to extract links / images / "
            "structured data from the email."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": (
                        "If set, click the matching card first. Get "
                        "ids from ``ebay_list_messages``."
                    ),
                },
                "max_text_chars": {
                    "type": "integer", "minimum": 100, "maximum": 50000,
                    "default": 8000,
                },
                "include_html": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        handler=_read_message,
        side="hybrid",
    )
)


# ---- ebay_navigate --------------------------------------------------------


async def _navigate(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url required"}
    try:
        info = await call_browser("ebay_navigate", {"url": url}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="ebay_navigate",
        description=(
            "Navigate the user's eBay tab to a different eBay URL. "
            "Refuses cross-origin destinations.\n"
            "\n"
            "Common paths:\n"
            "  • Search: /sch/i.html?_nkw=<keywords>&_sop=12 "
            "    (sort 12 = newest; 15 = price+shipping low to high)\n"
            "  • Item:   /itm/<numeric_id>\n"
            "  • Seller: /usr/<username>\n"
            "  • Category: /b/<slug>/<numeric_id>\n"
            "\n"
            "After navigation, the bookmarklet survives — call "
            "ebay_get_page_context again to confirm where the user is."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "ebay.com URL or path (e.g. '/itm/12345')"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=_navigate,
        side="hybrid",
    )
)
