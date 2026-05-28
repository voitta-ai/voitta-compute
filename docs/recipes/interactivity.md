# Recipe: Interactivity

Vanilla JS, Alpine.js, and other lightweight patterns work in the iframe. The screenshot captures one frame, so interactivity is for the user's exploration — not what gets frozen in the screenshot.

## Vanilla JS: toggle

```python
def build(ctx):
    return """<!DOCTYPE html>
<html>
<head>
<style>
  body { font-family: sans-serif; padding: 16px; }
  .section { display: none; border: 1px solid #ddd; padding: 12px; margin-top: 8px; border-radius: 4px; }
  .section.active { display: block; }
  button { margin-right: 8px; padding: 6px 12px; cursor: pointer; }
</style>
</head>
<body>
<button onclick="show('a')">Section A</button>
<button onclick="show('b')">Section B</button>
<div id="a" class="section active"><p>Content A</p></div>
<div id="b" class="section"><p>Content B</p></div>
<script>
  function show(id) {
    document.querySelectorAll('.section').forEach(el => el.classList.remove('active'));
    document.getElementById(id).classList.add('active');
  }
</script>
</body></html>"""
```

## Alpine.js: reactive UI

Alpine.js is ~15 KB and lets you write reactive components inline.

```python
import json

def build(ctx):
    items = [
        {"name": "Widget A", "status": "active",   "value": 142},
        {"name": "Widget B", "status": "inactive", "value": 87},
        {"name": "Widget C", "status": "active",   "value": 231},
    ]
    items_json = json.dumps(items)

    return f"""<!DOCTYPE html>
<html>
<head>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"></script>
<style>
  body {{ font-family: sans-serif; padding: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 8px; border-bottom: 1px solid #eee; text-align: left; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; }}
  .active {{ background: #d1fae5; color: #065f46; }}
  .inactive {{ background: #fee2e2; color: #991b1b; }}
</style>
</head>
<body>
<div x-data="{{
  items: {items_json},
  filter: 'all',
  get filtered() {{
    if (this.filter === 'all') return this.items;
    return this.items.filter(i => i.status === this.filter);
  }}
}}">
  <div style="margin-bottom:12px">
    Filter:
    <select x-model="filter">
      <option value="all">All</option>
      <option value="active">Active</option>
      <option value="inactive">Inactive</option>
    </select>
  </div>
  <table>
    <thead><tr><th>Name</th><th>Status</th><th>Value</th></tr></thead>
    <tbody>
      <template x-for="item in filtered" :key="item.name">
        <tr>
          <td x-text="item.name"></td>
          <td><span class="badge" :class="item.status" x-text="item.status"></span></td>
          <td x-text="item.value"></td>
        </tr>
      </template>
    </tbody>
  </table>
</div>
</body></html>"""
```

## Vanilla JS: sortable table

```python
import json

def build(ctx):
    rows = [["Alice", 95], ["Bob", 72], ["Carol", 88], ["Dave", 61]]
    rows_json = json.dumps(rows)

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ font-family: sans-serif; padding: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ cursor: pointer; background: #f0f0f0; padding: 8px; text-align: left; user-select: none; }}
  th:hover {{ background: #e0e0e0; }}
  td {{ padding: 8px; border-bottom: 1px solid #eee; }}
</style>
</head>
<body>
<table id="t">
  <thead><tr>
    <th onclick="sort(0)">Name ↕</th>
    <th onclick="sort(1)">Score ↕</th>
  </tr></thead>
  <tbody></tbody>
</table>
<script>
  var data = {rows_json};
  var asc = [true, true];
  function render() {{
    document.querySelector('#t tbody').innerHTML =
      data.map(r => '<tr><td>' + r[0] + '</td><td>' + r[1] + '</td></tr>').join('');
  }}
  function sort(col) {{
    data.sort((a, b) => asc[col] ? (a[col] > b[col] ? 1 : -1) : (a[col] < b[col] ? 1 : -1));
    asc[col] = !asc[col];
    render();
  }}
  render();
</script>
</body></html>"""
```

## Notes

- The iframe is yours — any JavaScript that works in a browser works here.
- htmx works too, but requests go to the page origin, not the backend. Use carefully.
- Keep heavy dependencies (React, Vue) off report iframes — they add load time and complexity. Alpine.js is usually sufficient.
