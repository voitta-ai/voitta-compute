"""Convert Runpod's Mintlify MDX docs into plain Markdown for the RAG index.

Run at INSTALL time by ``installer.sync_plugin_docs`` against a shallow clone
of https://github.com/runpod/docs — see ``manifest.json``'s ``docs_repo``.
The entry point is :func:`convert_tree`, which the installer resolves from
``"transform": "convert.py:convert_tree"``.

Why a converter rather than indexing the repo as-is: the pages are ``.mdx``,
not Markdown. They carry ESM imports, JSX components (``<Steps>``, ``<Card>``,
``<ParamField>``…) and ``<Tooltip/>`` references whose text lives in a shared
``snippets/tooltips.jsx``. Indexed raw, a chunk reads as markup; worse, the
tooltips render as nothing, so "resources for AI <TrainingTooltip/>,
<FineTuningTooltip/>, rendering" indexes as "resources for AI , , rendering".

The module is deliberately dependency-free (stdlib only) and self-contained:
the installer loads it by file path, outside any package.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap
from pathlib import Path

SOURCE_BASE = "https://docs.runpod.io"

# Directories in the upstream repo that are not product documentation.
SKIP_DIRS = {".git", ".github", ".claude", ".cursor", "tests", "snippets", "helpers"}

# Components rewritten into Markdown. Anything outside this set is left alone,
# which is what protects literal placeholders that appear in prose —
# <endpoint>, <name>, <worker>, <NAME> — from being eaten as markup.
COMPONENTS = {
    "Note", "Tip", "Warning", "Info", "Check", "Danger", "Important", "Caution",
    "Steps", "Step", "Tabs", "Tab", "Accordion", "AccordionGroup",
    "Card", "CardGroup", "Columns", "Frame", "CodeGroup",
    "ParamField", "ResponseField", "RequestExample", "ResponseExample",
    "Tooltip", "Badge", "Expandable", "Update", "Snippet",
    "Tree", "Tree.Folder", "Tree.File",
}

_NAME = r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)?"
_FENCE_RE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")
_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|\{([^}]*)\})""")


# --------------------------------------------------------------- code masking

def _mask_code(text: str) -> tuple[str, list[str]]:
    """Swap fenced blocks for placeholders so no JSX rule can touch code.

    Runs first, which is also what keeps ``import requests`` inside a Python
    example from being mistaken for an ESM import and stripped.
    """
    out: list[str] = []
    blocks: list[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent, fence, _info = m.groups()
        buf = [lines[i]]
        i += 1
        closer = re.compile(r"^[ \t]*" + fence[0] + "{" + str(len(fence)) + ",}[ \t]*$")
        while i < len(lines) and not closer.match(lines[i]):
            buf.append(lines[i])
            i += 1
        if i < len(lines):
            buf.append(lines[i])
            i += 1
        # Store flush-left so the placeholder dedents with its surrounding
        # prose when a component wrapper is unwrapped.
        if indent:
            buf = [ln[len(indent):] if ln.startswith(indent) else ln.lstrip() for ln in buf]
        blocks.append("\n".join(buf))
        out.append(f"{indent}\x00CODE{len(blocks) - 1}\x00")
    return "\n".join(out), blocks


def _clean_fence_info(block: str) -> str:
    """```python Python theme={…}  ->  ```python"""
    lines = block.split("\n")
    m = _FENCE_RE.match(lines[0])
    if not m:
        return block
    indent, fence, info = m.groups()
    info = re.sub(r"\s*\w+=\{.*?\}\s*", " ", info).strip()
    lang = info.split()[0] if info else ""
    if lang and not re.fullmatch(r"[A-Za-z0-9_+#.-]+", lang):
        lang = ""
    lines[0] = f"{indent}{fence}{lang}"
    return "\n".join(lines)


def _unmask_code(text: str, blocks: list[str]) -> str:
    def sub(m: re.Match) -> str:
        pad, block = m.group(1), _clean_fence_info(blocks[int(m.group(2))])
        if not pad:
            return block
        # The prefix is usually indentation, but for a fence inside a callout
        # it is "> " — every line needs it or the block escapes the quote.
        return "\n".join(
            (pad + ln if ln.strip() else pad.rstrip()) for ln in block.split("\n")
        )

    return re.sub(r"^([ \t>]*)\x00CODE(\d+)\x00", sub, text, flags=re.M)


# ------------------------------------------------------------------ tooltips

def load_tooltips(repo: Path) -> dict[str, str]:
    """Map ``FooTooltip`` -> display word from ``snippets/tooltips.jsx``.

    Upstream keeps every definition in that one file and imports it per page,
    so this is read once per run rather than per document.
    """
    out: dict[str, str] = {}
    snippet = repo / "snippets" / "tooltips.jsx"
    if not snippet.is_file():
        return out
    text = snippet.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r"export\s+const\s+(\w+)\s*=", text):
        name = m.group(1)
        tail = text[m.end():]
        inner = re.search(r"<Tooltip[^>]*>(.*?)</Tooltip>", tail, re.S)
        nxt = re.search(r"export\s+const\s+\w+\s*=", tail)
        if inner and (not nxt or inner.start() < nxt.start()):
            out[name] = inner.group(1).strip()
    return out


def apply_tooltips(text: str, mapping: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in mapping:
            return mapping[name]
        # Unknown tooltip: de-camel-case it rather than leave a dangling tag.
        return re.sub(r"(?<!^)(?=[A-Z])", " ", re.sub(r"Tooltip$", "", name)).lower()

    return re.sub(rf"<(\w+Tooltip)\s*/>", sub, text)


def collect_glossary(repo: Path) -> str:
    """Build one page from the tooltip definitions.

    Inline tooltips collapse to their display word, which keeps prose readable
    but drops the definition text. The terms repeat across pages, so they are
    gathered here once instead of inlined everywhere.
    """
    snippet = repo / "snippets" / "tooltips.jsx"
    if not snippet.is_file():
        return ""
    text = snippet.read_text(encoding="utf-8", errors="replace")
    entries: dict[str, tuple[str, str]] = {}
    for m in re.finditer(r"<Tooltip\s+([^>]*?)>", text):
        a = parse_attrs(m.group(1))
        if a.get("headline") and a.get("tip"):
            entries.setdefault(a["headline"], (a["tip"], a.get("href", "")))
    if not entries:
        return ""

    lines = [
        "---",
        'title: "Glossary"',
        'description: "Runpod terminology, collected from the inline tooltip '
        'definitions in the documentation."',
        f"source_url: {SOURCE_BASE}",
        "---",
        "",
        "# Glossary",
        "",
        "Terms defined in tooltips throughout the Runpod documentation.",
        "",
    ]
    for term in sorted(entries, key=str.lower):
        tip, href = entries[term]
        link = f" See [{term}]({SOURCE_BASE}{href})." if href.startswith("/") else ""
        lines.append(f"## {term}\n\n{tip}{link}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------- attributes

def parse_attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(raw):
        out[m.group(1)] = next(v for v in m.groups()[1:] if v is not None)
    for m in re.finditer(r"(?:^|\s)(\w+)(?=\s|$)", _ATTR_RE.sub(" ", raw)):
        out.setdefault(m.group(1), "true")
    return out


# ----------------------------------------------------------- component parse

def _find_block(text: str, start: int):
    """Parse a component opening at ``start`` -> (name, attrs, inner, end)."""
    m = re.compile(
        rf"<({_NAME})((?:[^<>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>"
    ).match(text, start)
    if not m:
        return None
    name, raw_attrs, selfclose = m.group(1), m.group(2), m.group(3)
    if name not in COMPONENTS:
        return None
    attrs = parse_attrs(raw_attrs)
    if selfclose:
        return name, attrs, "", m.end()

    depth = 1
    pos = m.end()
    tag_re = re.compile(
        rf"<(/?)({re.escape(name)})((?:[^<>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>"
    )
    while depth and pos < len(text):
        t = tag_re.search(text, pos)
        if not t:
            return None  # unbalanced — leave the source alone
        if t.group(1) == "/":
            depth -= 1
        elif not t.group(4):
            depth += 1
        pos = t.end()
        if depth == 0:
            return name, attrs, text[m.end(): t.start()], t.end()
    return None


def _dedent(s: str) -> str:
    """Strip the indentation a Mintlify body carries inside its wrapper."""
    s = s.strip("\n")
    return textwrap.dedent(s).strip("\n") if s.strip() else ""


def render(name: str, attrs: dict[str, str], inner: str) -> str:
    body = _dedent(inner)
    title = attrs.get("title") or attrs.get("header") or ""

    if name in {"Note", "Tip", "Warning", "Info", "Check", "Danger",
                "Important", "Caution"}:
        label = "Success" if name == "Check" else name
        head = f"**{label}:** {title}".rstrip() if title else f"**{label}**"
        return "\n".join(f"> {ln}".rstrip() for ln in [head, ""] + body.split("\n"))

    if name in {"Steps", "Tabs", "AccordionGroup", "CardGroup", "Columns",
                "Frame", "CodeGroup", "Expandable", "Snippet", "Tree"}:
        return body

    if name in {"Step", "Tab", "Accordion", "Update"}:
        return f"**{title}**\n\n{body}" if title else body

    if name == "Card":
        href = attrs.get("href", "")
        if title and href:
            link = f"{SOURCE_BASE}{href}" if href.startswith("/") else href
            return f"**[{title}]({link})**\n\n{body}".strip()
        return f"**{title}**\n\n{body}".strip() if title else body

    if name in {"ParamField", "ResponseField"}:
        ident = (attrs.get("body") or attrs.get("name") or attrs.get("query")
                 or attrs.get("path") or "")
        bits = []
        if attrs.get("type"):
            bits.append(f"*{attrs['type']}*")
        if attrs.get("required") == "true":
            bits.append("**required**")
        if attrs.get("default"):
            bits.append(f"default: `{attrs['default']}`")
        meta = " — " + ", ".join(bits) if bits else ""
        head = f"- **`{ident}`**{meta}" if ident else f"-{meta}"
        return head + ("\n\n" + textwrap.indent(body, "  ") if body else "")

    if name == "RequestExample":
        return f"**Request example**\n\n{body}"
    if name == "ResponseExample":
        return f"**Response example**\n\n{body}"
    if name == "Tooltip":
        return body or attrs.get("tip", "")
    if name == "Badge":
        return f"**{body}**" if body else ""

    if name == "Tree.Folder":
        comment = f" — {attrs['comment']}" if attrs.get("comment") else ""
        head = f"- `{attrs.get('name', '')}/`{comment}"
        return head + ("\n" + textwrap.indent(body, "  ") if body else "")
    if name == "Tree.File":
        comment = f" — {attrs['comment']}" if attrs.get("comment") else ""
        return f"- `{attrs.get('name', '')}`{comment}"

    return body


def convert_jsx(text: str) -> str:
    """Rewrite every whitelisted component, innermost content first."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text.find("<", i)
        if ch == -1:
            out.append(text[i:])
            break
        out.append(text[i:ch])
        found = _find_block(text, ch)
        if found is None:
            out.append("<")
            i = ch + 1
            continue
        name, attrs, inner, end = found
        rendered = render(name, attrs, convert_jsx(inner))
        if name in {"Tooltip", "Badge"}:
            out.append(rendered)                       # inline
        elif name.startswith("Tree."):
            out.append("\n" + rendered + "\n")         # list siblings stay tight
        else:
            out.append("\n\n" + rendered + "\n\n")
        i = end
    return "".join(out)


# -------------------------------------------------------------------- tidy up

def _img(m: re.Match) -> str:
    attrs = parse_attrs(m.group(1))
    src, alt = attrs.get("src", ""), attrs.get("alt", "")
    return f"![{alt}]({src})" if src else ""


def tidy(text: str) -> str:
    # ESM imports — safe here because code fences are already masked.
    text = re.sub(r"^\s*import\s+.*?\s+from\s+['\"].*?['\"];?\s*$", "", text, flags=re.M)
    text = re.sub(r"^\s*export\s+const\s+\w+\s*=.*?;\s*$", "", text, flags=re.M | re.S)
    text = re.sub(r"<div[^>]*/>", "", text)
    text = re.sub(r"</?div[^>]*>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<kbd>(.*?)</kbd>", lambda m: f"`{m.group(1).strip()}`", text, flags=re.S)
    text = re.sub(r"<h4>\s*", "\n\n#### ", text)
    text = re.sub(r"[ \t]*</h4>[ \t]*\n?[ \t]*", "\n\n", text)
    text = re.sub(r"<img([^>]*?)/?>", _img, text)
    text = re.sub(
        r"<iframe[^>]*src=\"([^\"]+)\"[^>]*>(?:</iframe>)?",
        lambda m: f"[Embedded video]({m.group(1)})", text,
    )
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)   # MDX comments
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# ------------------------------------------------------------------ OpenAPI

# The API-reference pages are not prose: each is a stub whose front matter says
# ``openapi: post /v2/pods``, and Mintlify renders the parameter and response
# tables from the sibling openapi.json at site-build time. Converting the stub
# alone yields ~250 bytes of front matter and nothing to retrieve, so the
# operation is rendered from the spec here instead.

_MAX_REF_DEPTH = 6


def load_openapi(repo: Path) -> dict[str, dict]:
    """Map the directory holding each ``openapi.json`` to its parsed spec."""
    specs: dict[str, dict] = {}
    for path in sorted(repo.rglob("openapi.json")):
        rel = path.relative_to(repo)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            specs[rel.parent.as_posix()] = json.loads(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except Exception:
            continue
    return specs


def _resolve(node, spec: dict, depth: int = 0):
    """Follow ``$ref`` into components, with a depth cap for recursive schemas."""
    if depth > _MAX_REF_DEPTH or not isinstance(node, dict):
        return node if isinstance(node, dict) else {}
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    target: object = spec
    for part in ref[2:].split("/"):
        if not isinstance(target, dict):
            return {}
        target = target.get(part, {})
    merged = _resolve(target, spec, depth + 1) if isinstance(target, dict) else {}
    extra = {k: v for k, v in node.items() if k != "$ref"}
    return {**merged, **extra} if extra else merged


def _type_of(schema: dict) -> str:
    if not isinstance(schema, dict):
        return ""
    if schema.get("enum"):
        return " | ".join(f"`{v}`" for v in schema["enum"][:12])
    t = schema.get("type") or ""
    if t == "array":
        inner = schema.get("items") or {}
        it = inner.get("type") or inner.get("title") or ""
        return f"array[{it}]" if it else "array"
    for key in ("oneOf", "anyOf", "allOf"):
        if schema.get(key):
            return " / ".join(
                str(s.get("type") or s.get("title") or "object") for s in schema[key][:4]
            )
    return str(t)


def _render_schema(schema: dict, spec: dict, indent: int = 0, depth: int = 0) -> list[str]:
    """Flatten a JSON-schema object into an indented Markdown bullet list."""
    schema = _resolve(schema, spec, depth)
    if depth > _MAX_REF_DEPTH or not isinstance(schema, dict):
        return []

    for key in ("allOf", "oneOf", "anyOf"):
        if schema.get(key):
            lines: list[str] = []
            for sub in schema[key][:4]:
                lines += _render_schema(sub, spec, indent, depth + 1)
            return lines

    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(schema.get("required") or [])
    pad = "  " * indent
    out: list[str] = []
    for name, raw in props.items():
        sub = _resolve(raw if isinstance(raw, dict) else {}, spec, depth)
        bits = [b for b in (_type_of(sub),) if b]
        if name in required:
            bits.append("**required**")
        if sub.get("default") is not None:
            bits.append(f"default: `{sub['default']}`")
        meta = " — " + ", ".join(bits) if bits else ""
        desc = " ".join(str(sub.get("description") or "").split())
        out.append(f"{pad}- **`{name}`**{meta}" + (f" — {desc}" if desc else ""))
        if sub.get("type") == "object" or sub.get("properties"):
            out += _render_schema(sub, spec, indent + 1, depth + 1)
        elif sub.get("type") == "array":
            items = _resolve(sub.get("items") or {}, spec, depth)
            if items.get("properties"):
                out += _render_schema(items, spec, indent + 1, depth + 1)
    return out


def render_openapi(directive: str, specs: dict[str, dict], page_dir: str) -> str:
    """Render ``"post /v2/pods"`` into Markdown using the nearest spec."""
    parts = str(directive).split()
    if len(parts) < 2:
        return ""
    method, route = parts[0].lower(), parts[1]

    # Prefer the spec that sits in (or above) the page's own directory.
    candidates = [d for d in specs if page_dir == d or page_dir.startswith(d + "/")]
    candidates.sort(key=len, reverse=True)
    for key in candidates + sorted(specs):
        spec = specs[key]
        op = (spec.get("paths", {}).get(route, {}) or {}).get(method)
        if not isinstance(op, dict):
            continue

        out: list[str] = [f"`{method.upper()} {route}`", ""]
        if op.get("summary"):
            out += [f"**{op['summary']}**", ""]
        if op.get("description"):
            # Long-form Markdown — headings, tables, lists. Keep its line
            # structure; collapsing it to one line would fuse tables into prose.
            out += [str(op["description"]).strip(), ""]

        params = [_resolve(p, spec) for p in (op.get("parameters") or [])]
        for where, label in (("path", "Path parameters"),
                             ("query", "Query parameters"),
                             ("header", "Headers")):
            group = [p for p in params if p.get("in") == where]
            if not group:
                continue
            out += [f"## {label}", ""]
            for p in group:
                sub = _resolve(p.get("schema") or {}, spec)
                bits = [b for b in (_type_of(sub),) if b]
                if p.get("required"):
                    bits.append("**required**")
                meta = " — " + ", ".join(bits) if bits else ""
                desc = " ".join(str(p.get("description") or "").split())
                out.append(f"- **`{p.get('name', '')}`**{meta}" + (f" — {desc}" if desc else ""))
            out.append("")

        body = _resolve(op.get("requestBody") or {}, spec)
        content = (body.get("content") or {}).get("application/json") or {}
        if content.get("schema"):
            rows = _render_schema(content["schema"], spec)
            if rows:
                out += ["## Request body", ""] + rows + [""]

        responses = op.get("responses") or {}
        ok = [c for c in responses if str(c).startswith("2")]
        if ok:
            out += ["## Response", ""]
            for code in sorted(ok):
                resp = _resolve(responses[code], spec)
                desc = " ".join(str(resp.get("description") or "").split())
                out.append(f"**{code}** — {desc}" if desc else f"**{code}**")
                out.append("")
                rc = (resp.get("content") or {}).get("application/json") or {}
                if rc.get("schema"):
                    rows = _render_schema(rc["schema"], spec)
                    if rows:
                        out += rows + [""]
        errors = [str(c) for c in responses if str(c)[:1] in "45"]
        if errors:
            out += ["## Error responses", "",
                    ", ".join(f"`{c}`" for c in sorted(errors)), ""]

        # Runnable examples the spec ships per operation — the most directly
        # useful thing on an API page, so they go in rather than get dropped.
        samples = op.get("x-codeSamples") or op.get("x-code-samples") or []
        rendered_samples: list[str] = []
        for sample in samples:
            if not isinstance(sample, dict) or not sample.get("source"):
                continue
            lang = str(sample.get("lang") or "").lower()
            label = sample.get("label")
            if label:
                rendered_samples += [f"**{label}**", ""]
            rendered_samples += [f"```{lang}", str(sample["source"]).strip(), "```", ""]
        if rendered_samples:
            out += ["## Example", ""] + rendered_samples
        return "\n".join(out).strip() + "\n"
    return ""


def split_front_matter(text: str) -> tuple[str, str]:
    """Return (front_matter_body, rest). Upstream pages ship real YAML."""
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    return (m.group(1), text[m.end():]) if m else ("", text)


def convert_file(text: str, rel: Path) -> str:
    fm, body = split_front_matter(text)
    body, code = _mask_code(body)
    body = apply_tooltips(body, convert_file.tooltips)  # type: ignore[attr-defined]
    body = convert_jsx(body)
    body = tidy(body)
    body = _unmask_code(body, code)
    body = re.sub(r"\n{3,}", "\n\n", body)

    fm_lines = [ln for ln in fm.split("\n") if ln.strip()] if fm else []

    # An ``openapi:`` stub has no prose of its own — render the operation.
    directive = next(
        (ln.split(":", 1)[1].strip() for ln in fm_lines if ln.startswith("openapi:")),
        "",
    )
    if directive:
        rendered = render_openapi(
            directive, convert_file.specs, rel.parent.as_posix()  # type: ignore[attr-defined]
        )
        if rendered:
            body = (body.strip() + "\n\n" + rendered) if body.strip() else rendered

    url = f"{SOURCE_BASE}/{rel.with_suffix('').as_posix()}"
    if not any(ln.startswith("title:") for ln in fm_lines):
        h1 = re.search(r"^#\s+(.+)$", body, re.M)
        title = h1.group(1).strip() if h1 else rel.stem.replace("-", " ")
        fm_lines.insert(0, f'title: "{title}"')
    fm_lines = [ln for ln in fm_lines if not ln.startswith("source_url:")]
    fm_lines.append(f"source_url: {url}")
    return "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body.strip() + "\n"


convert_file.tooltips = {}  # type: ignore[attr-defined]
convert_file.specs = {}  # type: ignore[attr-defined]


# ------------------------------------------------------------- entry point

def convert_tree(src: Path, dst: Path) -> int:
    """Convert every ``.mdx`` page under ``src`` into Markdown under ``dst``.

    Returns the number of files written. Called by the installer; the return
    value gates the atomic swap, so a run that produces nothing leaves the
    previous good tree untouched.
    """
    src, dst = Path(src), Path(dst)
    convert_file.tooltips = load_tooltips(src)  # type: ignore[attr-defined]
    convert_file.specs = load_openapi(src)  # type: ignore[attr-defined]

    written = 0
    for path in sorted(src.rglob("*.mdx")):
        rel = path.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            out = convert_file(path.read_text(encoding="utf-8", errors="replace"), rel)
        except Exception:
            continue  # one bad page must not sink the whole corpus
        target = (dst / rel).with_suffix(".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(out, encoding="utf-8")
        written += 1

    glossary = collect_glossary(src)
    if glossary:
        (dst / "glossary.md").write_text(glossary, encoding="utf-8")
        written += 1

    return written


if __name__ == "__main__":  # manual runs: python convert.py <src> <dst>
    import sys

    if len(sys.argv) != 3:
        raise SystemExit("usage: convert.py <repo-checkout> <output-dir>")
    out_dir = Path(sys.argv[2])
    shutil.rmtree(out_dir, ignore_errors=True)
    print(f"wrote {convert_tree(Path(sys.argv[1]), out_dir)} file(s) to {out_dir}")
