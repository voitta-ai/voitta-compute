# Reports

A report is a user-authored Python script that produces an HTML string. The string is served in an iframe in the widget's report pane.

## Script contract

Scripts live at `~/Library/Application Support/Voitta Compute/backend/scripts/<name>/code.py`.

Every script must define a `build(ctx)` function at the top level. It must either:
- Return a raw HTML string, **or**
- Use `ctx` emitters (which produce inline chat content — see below). Returning `None` is fine when you only use emitters.

```python
def build(ctx):
    return "<h1>Hello</h1><p>World</p>"
```

## Execution model

- Scripts run in `asyncio.to_thread()` (a thread pool), never the main event loop.
- Hard timeout: **120 seconds**. A `TimeoutError` is surfaced to the model.
- `matplotlib` is switched to the `Agg` backend before user code runs (no GUI).
- The namespace has normal Python builtins. No import restrictions.
- The script is `compile()`d first — syntax errors are caught before execution.

## ctx API

`ctx` is a `ScriptContext` instance injected by the runner.

### Inputs

```python
ctx.args          # dict — forwarded from run_script(args={...})
ctx.host          # str | None — hostname of the user's current page
```

### Inline emitters

These surface content into the chat alongside the report pane.

```python
ctx.text("## Summary\nSome markdown")   # emit Markdown
ctx.image(fig_bytes, "image/png")        # emit base64 <img> (bytes or base64 str)
ctx.json({"key": "value"})              # emit collapsible JSON block
ctx.log("debug message")                # append to tool-result log lines
```

### Theme

```python
t = ctx.theme()   # dict: {"--voitta-bg": "#1a1a2e", "--voitta-accent": "#7c3aed", ...}
```

Returns CSS-variable name → value pairs from the active plugin's theme. Use to style your report consistently with the surrounding UI.

### Data access

```python
ctx.snapshot("handle")            # return python_storage snapshot record dict
ctx.file("handle")                # return Path to first data file in snapshot
ctx.file("handle", "data.csv")    # return Path to named file in snapshot
ctx.dataframe("handle")           # load curves.pkl as a pandas DataFrame
ctx.raw("handle")                 # load raw.json from snapshot, return parsed value
ctx.ensure_local("scheme://...")  # download upstream artefact ref, return local path
```

## Output: one format only

Reports produce **one thing**: a raw HTML string. Return it from `build(ctx)`.

```python
def build(ctx):
    import base64, io
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [4, 1, 3])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f'<html><body><img src="data:image/png;base64,{b64}"></body></html>'
```

The HTML is served at `/api/html-report?id=<slug>` and rendered in a same-origin `<iframe>`.

## Smoke testing

`define_script` and `edit_script` run a smoke test (`sandbox.smoke_test()`) before persisting the code. The script must not crash during a bare `build(ctx)` call. If it does, the error is returned to the model without saving.

## Script tools summary

| Tool | Action |
|---|---|
| `define_script(name, code)` | Create; smoke-test first |
| `edit_script(name, code)` | Replace source; smoke-test first |
| `run_script(name, args?, wait_s?)` | Execute; dispatch HTML to pane |
| `verify_script(name, code)` | Smoke-test without saving |
| `get_script(name)` | Read source |
| `get_script_errors(name)` | Read last runtime errors |
| `delete_script(name)` | Remove |
