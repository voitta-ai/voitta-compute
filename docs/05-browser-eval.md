# browser_eval — JavaScript execution in the user's tab

`browser_eval(js, await_ms?)` runs arbitrary JavaScript in the user's
currently-bookmarklet'd browser tab and returns the result.

## When to use it

- Read auth tokens, session data, cookies the page sets in JS
- Scrape DOM content not exposed by a plugin primitive
- Call the site's own internal APIs using the user's existing session
- Extract data from `window` globals the site exposes
- Pass extracted data to a Python script for processing

## Return value

```
{ok: true, result: <whatever you returned>, logs: [{level, args}], ms}
{ok: false, error: "eval_threw", message, stack, logs, ms}
```

Whatever you `return` from the JS body becomes `result`. Top-level
`await` works — the body is wrapped in an `async function`.

---

## Use cases

### Read an auth token from localStorage

```js
return localStorage.getItem('auth_token')
// or
return localStorage.getItem('token')
```

### Read all localStorage keys at once

```js
return Object.fromEntries(
  Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])
)
```

### Read a cookie value

```js
const get = name => document.cookie.split('; ')
  .find(r => r.startsWith(name + '='))?.split('=')[1]
return get('session_id')
```

### Call the site's API with the user's session

The page's `fetch` carries the user's cookies automatically (same-origin):

```js
const data = await fetch('/api/v1/user/profile').then(r => r.json())
return data
```

With a Bearer token from localStorage:

```js
const token = localStorage.getItem('auth_token')
const data = await fetch('https://api.site.com/v1/orders', {
  headers: { 'Authorization': `Bearer ${token}` }
}).then(r => r.json())
return data
```

### Read a window global the site sets

```js
return window.__INITIAL_STATE__   // React/Redux apps often expose this
return window.APP_CONFIG
```

### Scrape DOM content

```js
const rows = [...document.querySelectorAll('table.data-table tr')].map(tr =>
  [...tr.querySelectorAll('td')].map(td => td.innerText.trim())
)
return rows
```

---

## Pattern: browser → Python pipeline

This is the standard pattern for site-authenticated workflows that need
real data processing, file storage, or report generation.

**Step 1 — extract from browser:**
```
browser_eval: return localStorage.getItem('auth_token')
// → result: "eyJhbGciOi..."
```

**Step 2 — define a Python script that accepts it:**
```python
def build(ctx):
    token = ctx.args.get("token")
    if not token:
        return None   # smoke-test guard

    import httpx
    resp = httpx.get(
        "https://api.site.com/v1/data",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # ... process, chart, store, whatever
    ctx.json(data)
```

**Step 3 — run it with the token:**
```
run_script(name="site-data", args={"token": "<token from step 1>"})
```

The Python script runs on the backend server — it can call any external
API, write files, generate charts, query databases. The browser only
needs to supply the credential.

---

## Passing multiple values

Return an object from the browser, destructure in Python:

```js
// browser_eval
return {
  token: localStorage.getItem('auth_token'),
  userId: window.__USER__.id,
  orgId: window.__USER__.orgId,
}
```

```python
# in build(ctx)
token   = ctx.args.get("token")
user_id = ctx.args.get("userId")
org_id  = ctx.args.get("orgId")
```

---

## Timeout

Default is 30 seconds. For long-running page operations:

```
browser_eval(js="...", await_ms=60000)
```

Max is 120 seconds.

---

## What browser_eval cannot do

- Read `HttpOnly` cookies — browser-enforced, not accessible to JS
- Access other tabs or origins — same-origin policy applies
- Write files on the server — use `run_script` for that
