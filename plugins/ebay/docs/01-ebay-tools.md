# eBay plugin — tool catalog

Active only on `*.ebay.com`. No API key, no OAuth — every tool reads
the user's currently-loaded eBay page DOM via four browser primitives.

## Page-shape detector

eBay's URL → page mapping the plugin understands:

| Page | URL | `page_type` |
|---|---|---|
| Homepage | `/` | `home` |
| Search results | `/sch/i.html?_nkw=...` | `search` |
| Item details | `/itm/<id>` | `item` |
| Seller storefront | `/usr/<username>` | `seller` |
| Branded store | `/str/<slug>` | `store` |
| Category browse | `/b/<slug>/<id>` | `category` |
| My eBay | `/mye/...` | `myebay` |
| Anything else | — | `other` |

`ebay_get_page_context` returns this plus the search query, item id,
seller id, and any URL params — the LLM should call it first for any
eBay task.

## Tools

### `ebay_get_page_context`

Cheap probe. Always call first.

```
{ ok, url, page_type, title, search_query, item_id, seller_id_in_url, category_id_in_url, params }
```

### `ebay_scrape_search`

Reads result cards from a `/sch/` page. Modern eBay uses `.s-card`
elements with `data-listingid` on each; the plugin reads up to
`limit` (default 50, max 200) and skips eBay's "Shop on eBay"
placeholder ad by default.

Returns:
```
{ ok, search_query, total_dom_cards, returned, hits: [{ listing_id, title, price, href, image, subtitle }] }
```

Wrong page → `{ ok: false, error: "wrong_page" }`.

### `ebay_scrape_item`

Reads the active `/itm/<id>` page. Pulls the **schema.org Product
JSON-LD** when present (clean structured data: title, price, currency,
condition, image array, shipping, returns) plus DOM fallbacks for
fields the JSON-LD doesn't carry (seller display name, username from URL).

Returns:
```
{
  ok, item_id,
  has_jsonld_product, product_jsonld: {...},   // schema.org Product
  breadcrumbs: [...],                          // schema.org BreadcrumbList
  seller: { name, url, username },
  dom_title, dom_price, dom_condition, dom_item_location,
}
```

The JSON-LD is the canonical source — DOM fields are belt-and-braces
for the rare item that doesn't ship JSON-LD.

### `ebay_navigate`

Sends the user's eBay tab to a different eBay URL. Refuses
cross-origin destinations (the bookmarklet only attaches to ebay.com).

```
{ ok, navigating_to }
```

## Idiomatic flows

### "Find me cheap headphones, look at the top result"

```
1. ebay_navigate(url="/sch/i.html?_nkw=headphones&_sop=15")     # sort: low price first
2. ebay_get_page_context()                                       # confirms page_type=search
3. ebay_scrape_search(limit=10)                                  # top 10 hits
4. ebay_navigate(url=hits[0].href)                               # dive into first
5. ebay_get_page_context()                                       # confirms page_type=item
6. ebay_scrape_item()                                            # full structured details
```

### "What is this listing?"

The user is already on `/itm/<id>` — single call:

```
1. ebay_scrape_item()
```

### "Compare three items"

```
For each itemId:
  ebay_navigate(url=f"/itm/{itemId}")
  ebay_scrape_item()
  store result.product_jsonld
Then summarise — prices, conditions, sellers — without storing
anything in python_storage (the JSON-LD blocks are small enough).
```

## Why no API

eBay has Browse API and Finding API but both require an OAuth client
ID + a developer account. The bookmarklet has no API credentials.
DOM-scraping the user's already-authenticated session covers every
research workflow at zero setup cost; the tradeoff is fragility to
eBay layout changes.

If eBay ships a layout change that breaks `.s-card` selectors,
`ebay_get_page_context` still works (URL parsing is forever) and
`ebay_scrape_item` keeps working (JSON-LD is a stable contract eBay
ships for SEO and won't drop). Only `ebay_scrape_search` is exposed
to layout drift, and the fix is one CSS-class update in
`plugins/ebay/frontend/widget.ts`.

## Limitations

- No buy/bid/watch — read-only.
- No saved searches, no notifications, no history.
- No price history (eBay doesn't expose it on item pages).
- Only the visible result page — no deep pagination. Walk
  pagination by parsing the next-page URL from the page and feeding
  it to `ebay_navigate`.
