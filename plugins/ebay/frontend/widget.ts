// eBay plugin — frontend primitives. Side-effect import; voitta core
// globs every plugin's frontend/widget.ts and bundles them into
// widget.js.
//
// Three primitives, all DOM-scraping the active eBay tab:
//
//   • ebay_inspect_page       — what page is the user on?
//   • ebay_scrape_search      — read result cards from a /sch/ page
//   • ebay_scrape_item        — read schema.org Product JSON-LD + DOM
//                                 from an /itm/ page
//
// No API key, no auth. Whatever the page renders, we read.

import { PrimitiveError, registerPrimitive } from "../../../frontend/src/lib/bridge";


// ---- helpers --------------------------------------------------------------


function _classifyPage(): string {
  const p = location.pathname;
  if (p === "/" || p === "/index") return "home";
  if (p.startsWith("/sch/")) return "search";
  if (p.match(/^\/itm\/\d+/)) return "item";
  if (p.startsWith("/usr/")) return "seller";
  if (p.startsWith("/str/")) return "store";
  if (p.startsWith("/b/")) return "category";
  if (p.startsWith("/mye/")) return "myebay";
  return "other";
}


function _itemIdFromUrl(href: string): string | null {
  const m = href.match(/\/itm\/(\d+)/);
  return m ? m[1] : null;
}


function _parseJsonLd(): unknown[] {
  const out: unknown[] = [];
  for (const s of Array.from(document.querySelectorAll('script[type="application/ld+json"]'))) {
    try {
      const v = JSON.parse(s.textContent || "");
      if (Array.isArray(v)) out.push(...v);
      else out.push(v);
    } catch { /* skip malformed */ }
  }
  return out;
}


// ---- ebay_inspect_page ----------------------------------------------------
//
// Lightweight "what is the user looking at" probe — analogous to
// drive_get_page_context. Always cheap; first call the LLM should make.

registerPrimitive("ebay_inspect_page", async () => {
  const params = Object.fromEntries(new URLSearchParams(location.search));
  return {
    url: location.href,
    pathname: location.pathname,
    title: document.title,
    page_type: _classifyPage(),
    search_query: typeof params._nkw === "string" ? params._nkw : null,
    item_id: _itemIdFromUrl(location.href),
    seller_id_in_url: location.pathname.match(/\/usr\/([^/]+)/)?.[1] || null,
    category_id_in_url: location.pathname.match(/\/b\/[^/]+\/(\d+)/)?.[1] || null,
    params,
  };
});


// ---- ebay_scrape_search ---------------------------------------------------
//
// Read result cards from a /sch/ page. Modern eBay uses ``.s-card``
// (legacy is ``.s-item``) — we handle both. Drops the "Shop on eBay"
// placeholder ad eBay always inserts at position 1-2 of every search
// (recognisable by the literal title "Shop on eBay").

interface SearchHit {
  listing_id: string | null;
  title: string;
  price: string | null;
  href: string | null;
  image: string | null;
  subtitle: string | null;
}


registerPrimitive("ebay_scrape_search", async (rawArgs) => {
  if (_classifyPage() !== "search") {
    throw new PrimitiveError(
      "wrong_page",
      `current page (${_classifyPage()}) is not a search results page; navigate to /sch/i.html?...`,
    );
  }
  const limit = Math.max(1, Math.min(200, Number(rawArgs?.limit ?? 50)));
  const include_ads = Boolean(rawArgs?.include_ads ?? false);

  // Modern eBay first, fall back to the legacy class.
  let cards = Array.from(document.querySelectorAll(".s-card")) as HTMLElement[];
  if (cards.length === 0) {
    cards = Array.from(document.querySelectorAll(".s-item")) as HTMLElement[];
  }

  const out: SearchHit[] = [];
  for (const card of cards) {
    if (out.length >= limit) break;
    const title = (
      card.querySelector(".s-card__title") ||
      card.querySelector(".s-item__title") ||
      card.querySelector("h3, h2, [class*='title']")
    )?.textContent?.trim() ?? "";
    if (!title) continue;
    if (!include_ads && title === "Shop on eBay") continue;

    const href = (card.querySelector("a.s-card__link, a.s-item__link, a[href*='/itm/']") as HTMLAnchorElement | null)?.href ?? null;
    const cleanHref = href ? href.split("?")[0] : null;

    out.push({
      listing_id: card.getAttribute("data-listingid"),
      title: title.replace(/^New Listing/, "").replace(/Opens in a new window or tab$/, "").trim(),
      price: (
        card.querySelector(".s-card__price") ||
        card.querySelector(".s-item__price")
      )?.textContent?.trim() ?? null,
      href: cleanHref,
      image: (card.querySelector("img.s-card__image, img[src]") as HTMLImageElement | null)?.src ?? null,
      subtitle: (
        card.querySelector(".s-card__subtitle") ||
        card.querySelector(".s-item__subtitle")
      )?.textContent?.trim() ?? null,
    });
  }

  return {
    url: location.href,
    search_query: new URLSearchParams(location.search).get("_nkw"),
    total_dom_cards: cards.length,
    returned: out.length,
    hits: out,
  };
});


// ---- ebay_scrape_item -----------------------------------------------------
//
// Read an /itm/<id> page. Pulls the schema.org Product JSON-LD when
// present (clean structured data) and merges in DOM-scraped fallbacks
// for fields the JSON-LD doesn't carry (seller name, item id from URL).

registerPrimitive("ebay_scrape_item", async () => {
  if (_classifyPage() !== "item") {
    throw new PrimitiveError(
      "wrong_page",
      `current page (${_classifyPage()}) is not an item page; navigate to /itm/<id>`,
    );
  }
  const itemId = _itemIdFromUrl(location.href);
  const ld = _parseJsonLd();

  // Find the Product entry — JSON-LD blocks may be wrapped in arrays
  // or contain BreadcrumbList alongside.
  type LdNode = { "@type"?: string | string[] } & Record<string, unknown>;
  function flattenLd(node: unknown): LdNode[] {
    if (!node) return [];
    if (Array.isArray(node)) return node.flatMap(flattenLd);
    if (typeof node === "object") return [node as LdNode];
    return [];
  }
  const nodes = ld.flatMap(flattenLd);
  const product = nodes.find(n => {
    const t = n["@type"];
    return t === "Product" || (Array.isArray(t) && t.includes("Product"));
  }) as LdNode | undefined;

  const breadcrumbs = nodes.find(n => n["@type"] === "BreadcrumbList") as LdNode | undefined;

  const sellerLink = document.querySelector('a[href*="/usr/"]') as HTMLAnchorElement | null;
  const sellerName = document.querySelector(".x-sellercard-atf__info__about-seller a, .x-sellercard-atf__info__about-seller h2")?.textContent?.trim() ?? null;

  return {
    url: location.href,
    item_id: itemId,
    has_jsonld_product: !!product,
    product_jsonld: product ?? null,
    breadcrumbs: breadcrumbs?.itemListElement ?? null,
    seller: {
      name: sellerName,
      url: sellerLink?.href ?? null,
      // Username from URL, e.g. /usr/some_user
      username: sellerLink?.href?.match(/\/usr\/([^/?#]+)/)?.[1] ?? null,
    },
    // DOM fallbacks for the rare case JSON-LD is missing
    dom_title: document.querySelector("h1.x-item-title__mainTitle, h1[class*='item-title']")?.textContent?.trim() ?? null,
    dom_price: document.querySelector(".x-price-primary span.ux-textspans, [class*='price-primary']")?.textContent?.trim() ?? null,
    dom_condition: document.querySelector(".x-item-condition-text .ux-textspans, [class*='condition-text']")?.textContent?.trim() ?? null,
    dom_item_location: document.querySelector(".ux-labels-values--itemLocation .ux-textspans")?.textContent?.trim() ?? null,
  };
});


// ---- ebay_scrape_myebay ---------------------------------------------------
//
// Read the dashboard at /mye/myebay/summary. The page hosts several
// distinct modules; we extract the two most useful: Recently Viewed
// items and recent Orders. Other modules (Watchlist, Bids/Offers,
// Saved Searches, Saved Sellers, Coupons) live under their own
// sub-routes (/mye/myebay/watchlist etc) — visible from the sidebar
// but render different DOMs we'd need to scrape per-page.

interface RecentItem {
  title: string;
  price: string | null;
  shipping: string | null;
  href: string | null;
  image: string | null;
  item_id: string | null;
}

interface OrderRecord {
  status: string | null;       // "Delivered", "In transit", etc
  order_date: string | null;
  order_total: string | null;
  order_number: string | null;
  details_url: string | null;
  listing_id: string | null;
  item_title: string | null;
  item_image: string | null;
}

function _findSectionByHeading(pattern: RegExp): HTMLElement | null {
  for (const h of Array.from(document.querySelectorAll("h1, h2, h3"))) {
    if (pattern.test((h.textContent || "").trim())) {
      return h.closest("section, .m-container-section") as HTMLElement | null;
    }
  }
  return null;
}

function _scrapeRecentlyViewed(): RecentItem[] {
  const section = _findSectionByHeading(/^recently viewed/i);
  if (!section) return [];
  const out: RecentItem[] = [];
  // Each item is a SECTION carrying classes like "NJI_ EU3n" — selector-
  // generated, so we anchor on having an inner h3 + an inner anchor with
  // an /itm/<id> href. That's resilient if the random class names change.
  const cards = Array.from(section.querySelectorAll("section")) as HTMLElement[];
  for (const c of cards) {
    const title = c.querySelector("h3")?.textContent?.trim();
    const link = c.querySelector('a[href*="/itm/"]') as HTMLAnchorElement | null;
    if (!title || !link) continue;
    const href = link.href;
    out.push({
      title,
      price: c.querySelector('[role="text"]')?.textContent?.trim() ?? null,
      shipping: Array.from(c.querySelectorAll('[role="text"]'))
        .map(s => (s.textContent || "").trim())
        .find(t => /delivery|shipping/i.test(t)) ?? null,
      href: href.split("?")[0],
      image: (c.querySelector("img") as HTMLImageElement | null)?.src ?? null,
      item_id: href.match(/\/itm\/(\d+)/)?.[1] ?? null,
    });
  }
  return out;
}

function _scrapeOrders(limit: number): OrderRecord[] {
  const cards = Array.from(document.querySelectorAll(".m-order-card")) as HTMLElement[];
  const out: OrderRecord[] = [];
  for (const card of cards) {
    if (out.length >= limit) break;
    // Status: the first ``primary__item--item-text`` in the
    // primaryMessage span, e.g. "Delivered".
    const status = card.querySelector(".primaryMessage .primary__item--item-text")
      ?.textContent?.trim() ?? null;

    // Order metadata uses label/value pairs. Walk every wrapper and
    // pair the label text with its sibling.
    const orderDate = _findOrderField(card, /order date/i);
    const orderTotal = _findOrderField(card, /order total/i);
    const orderNumber = _findOrderField(card, /order number/i);

    const detailsLink = card.querySelector('a[href*="order.ebay.com"]') as HTMLAnchorElement | null;
    const itemRow = card.querySelector("[input-listing-id]");
    const listingId = itemRow?.getAttribute("input-listing-id") ?? null;
    const itemTitleLink = card.querySelector(".clipped");
    const itemImg = card.querySelector(".container-item-col-img img") as HTMLImageElement | null;

    out.push({
      status,
      order_date: orderDate,
      order_total: orderTotal,
      order_number: orderNumber,
      details_url: detailsLink?.href?.split("&")[0] ?? null,
      listing_id: listingId,
      item_title: itemTitleLink?.textContent?.trim() ?? null,
      item_image: itemImg?.src ?? null,
    });
  }
  return out;
}

function _findOrderField(card: HTMLElement, labelRe: RegExp): string | null {
  for (const wrap of Array.from(card.querySelectorAll(".primary__item--wrapper"))) {
    const texts = Array.from(wrap.querySelectorAll(".primary__item--item-text"));
    if (texts.length < 2) continue;
    if (labelRe.test((texts[0].textContent || "").trim())) {
      return (texts[1].textContent || "").trim();
    }
  }
  return null;
}

registerPrimitive("ebay_scrape_myebay", async (rawArgs) => {
  if (_classifyPage() !== "myebay") {
    throw new PrimitiveError(
      "wrong_page",
      `current page (${_classifyPage()}) is not a My eBay page; navigate to /mye/myebay/summary`,
    );
  }
  const orderLimit = Math.max(1, Math.min(200, Number(rawArgs?.order_limit ?? 50)));

  const recent = _scrapeRecentlyViewed();
  const orders = _scrapeOrders(orderLimit);

  return {
    url: location.href,
    pathname: location.pathname,
    page: location.pathname.match(/\/mye\/myebay\/([^/?#]+)/)?.[1] ?? "summary",
    recently_viewed: recent,
    recently_viewed_count: recent.length,
    orders,
    orders_returned: orders.length,
    orders_total_in_dom: document.querySelectorAll(".m-order-card").length,
  };
});


// ---- ebay_navigate --------------------------------------------------------
//
// Internal navigation helper — same-origin replacement for
// `location.href = ...` from the LLM. Refuses cross-origin navigation
// so the bookmarklet doesn't accidentally lose its session by sending
// the user away from ebay.com.

registerPrimitive("ebay_navigate", async (rawArgs) => {
  const target = String(rawArgs?.url ?? "");
  let parsed: URL;
  try {
    parsed = new URL(target, location.origin);
  } catch {
    throw new PrimitiveError("invalid_url", `not a valid URL: ${target}`);
  }
  if (!parsed.hostname.endsWith("ebay.com")) {
    throw new PrimitiveError(
      "cross_origin",
      `refusing to navigate to non-ebay host ${parsed.hostname}`,
    );
  }
  location.href = parsed.href;
  return { ok: true, navigating_to: parsed.href };
});
