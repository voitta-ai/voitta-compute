# Recipe: Interactivity

Vanilla JS, htmx, Alpine.js — anything that runs in the browser
works. The iframe is yours; the screenshot happens at one moment
so interactivity is for the user's exploration, not what gets
captured.

## Vanilla JS

```python
return """<!doctype html>
<html>
<body>
  <button id="btn">Click me</button>
  <p id="msg"></p>
  <script>
    const counter = { n: 0 };
    document.getElementById("btn").addEventListener("click", () => {
      counter.n++;
      document.getElementById("msg").textContent = `Clicked ${counter.n}`;
    });
  </script>
</body>
</html>"""
```

## Alpine.js — declarative, no build step

```python
return """<!doctype html>
<html>
<head>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"></script>
</head>
<body>
  <div x-data="{ open: false, options: ['layered', 'stress', 'mrtree'], selected: 'layered' }">
    <button @click="open = !open" x-text="selected"></button>
    <ul x-show="open" @click.outside="open = false">
      <template x-for="o in options">
        <li @click="selected = o; open = false" x-text="o"></li>
      </template>
    </ul>
    <p>You picked: <strong x-text="selected"></strong></p>
  </div>
</body>
</html>"""
```

## htmx — server-side interactivity

For reports that need to fetch fresh data on user interaction.
The BE doesn't have a per-report state endpoint, so this is
limited — htmx is best when you have a separate API the iframe
can call.

```python
return """<!doctype html>
<html>
<head>
  <script src="https://unpkg.com/htmx.org@2.0.0"></script>
</head>
<body>
  <button hx-get="https://api.example.com/data"
          hx-target="#result">Load data</button>
  <div id="result"></div>
</body>
</html>"""
```

## What gets screenshotted

- The screenshot is a single rasterised frame
- Interactive state at screenshot time is captured (e.g. a
  dropdown if open)
- The LLM only sees the screenshot — not the interactivity
- The USER sees + uses the interactivity in the chat

So: build interactive things for the user to explore. Build
screenshot-friendly content (settled, static-frame) for the LLM
to verify.
