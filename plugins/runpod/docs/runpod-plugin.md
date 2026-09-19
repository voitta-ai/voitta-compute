# Runpod plugin

Two capabilities, both scoped to Runpod pages (`runpod.io`,
`console.runpod.io`, `docs.runpod.io`):

1. **Documentation** — the official Runpod docs, indexed into the RAG `docs`
   corpus at install time.
2. **API** — the full Runpod REST API, via Runpod's hosted MCP server.

No frontend widget, and no hand-written tools.

> **The API surface is unrestricted.** The agent can create, start, stop and
> delete pods, endpoints, clusters and volumes. Those operations cost money and
> some destroy running infrastructure. What actually limits it is the API key
> you supply — see [Scope and blast radius](#scope-and-blast-radius).

## API tools

The manifest declares an `mcp_servers` connector pointing at Runpod's hosted
MCP server, `https://mcp.getrunpod.io/`. Runpod generates that server's tools
from their v2 OpenAPI spec, so the tool list tracks their API without anything
here needing to change.

Tools appear as `runpod_<tool>` — `runpod_list-pods`, `runpod_create-pod`,
`runpod_pod-action` and so on. 72 tools at last count: 36 read-only
(`list-*` / `get-*`) and 36 that change state.

The manifest sets no `expose_tools` allowlist, so every tool the server
advertises is exposed — and new Runpod tools appear without a manifest change.

| Group | Examples |
|---|---|
| Pods | `list-pods`, `get-pod`, `create-pod`, `update-pod`, `delete-pod`, `pod-action` (start/stop), `stream-pod-logs` |
| Serverless | `list-endpoints`, `get-endpoint`, `update-endpoint`, `delete-endpoint`, `run-endpoint`, `runsync-endpoint`, `set-endpoint-gpus`, `purge-endpoint-queue`, `endpoint-health`, `retry-job`, `stream-job`, `stream-worker-logs` |
| Clusters | `list-clusters`, `get-cluster`, `update-cluster`, `delete-cluster`, `list-cluster-pods` |
| Storage | `list-network-volumes`, `update-network-volume`, `delete-network-volume` |
| Templates & registries | `list-templates`, `create-template`, `update-template`, `delete-template`, `create-registry`, `delete-registry` |
| Hub | `list-hub-repos`, `deploy-hub-repo` |
| Catalog | `list-gpu-types`, `list-cpu-types`, `list-data-centers`, `get-capacity` |
| Billing | `list-billing`, `list-pod-billing`, `list-endpoint-billing`, `list-serverless-billing` |
| Account | `get-ssh-keys`, `update-ssh-keys`, `list-delegations`, `revoke-delegation` |

### Setup

Settings → Plugins → **Runpod** card → chevron to expand → paste a Runpod API
key (mint one at console.runpod.io → Settings → API Keys) → Save. The status
badge turns green once the key authenticates; without one, tools stay
unavailable and the badge reads "unauth".

### Scope and blast radius

Since the plugin exposes everything, **the API key is the only thing that
bounds the agent**. Runpod's server acts with exactly that key's permissions:

- **Read-only key** — the mutating tools are still listed, but every call
  fails at Runpod's end. Reporting only, nothing can be spent or destroyed.
- **Read/write key** — the agent can deploy GPU pods (billed per second),
  stop and start them, and delete pods, endpoints, clusters and volumes.
  Deletions are not recoverable from here.

Pick the key to match how much autonomy you actually want. To constrain it in
the plugin instead, add an `expose_tools` array to the `mcp_servers` entry in
`manifest.json` naming only the tools you want reachable — absent means all.

Two tools are Runpod's own, not REST endpoints: `save_to_journal` /
`read_journal` (agent scratch memory held by Runpod) and `report_feedback`
(sends feedback to Runpod). Both send data to Runpod when called.

## What gets indexed

The manifest declares a `docs_repo`, so the installer clones
[runpod/docs](https://github.com/runpod/docs) — the Mintlify source behind
docs.runpod.io — at **install time** and converts it to Markdown:

```
plugin-docs-src/runpod/    shallow checkout of runpod/docs (never indexed)
plugin-docs/runpod/        converted Markdown (indexed into the docs corpus)
```

Both live beside `plugins/`, not inside it, because the launcher deletes and
re-seeds the plugin tree on every start.

Roughly 347 pages covering Pods, Serverless, endpoints, storage, networking,
pricing, the REST and GraphQL APIs, `runpodctl`, and the Python SDK, plus a
generated `glossary.md` built from the docs' tooltip definitions.

## Why it converts rather than copies

Upstream pages are `.mdx`, not Markdown. They carry ESM imports, JSX
components (`<Steps>`, `<Card>`, `<ParamField>`…) and `<Tooltip/>` references
whose visible text lives in a shared `snippets/tooltips.jsx`. Indexed raw, a
chunk reads as markup — and because tooltips render to nothing, a sentence
like "resources for AI `<TrainingTooltip/>`, `<FineTuningTooltip/>`,
rendering" would index as "resources for AI , , rendering".

`convert.py` resolves the tooltips, rewrites the component set into ordinary
Markdown, strips the imports, and keeps each page's existing YAML front
matter, adding a `source_url` back to docs.runpod.io.

## Querying the docs

The docs corpus is the default, so no `corpus` argument is needed:

```
rag_query(query="how do I configure a serverless endpoint worker")
```

Docs and API complement each other: `rag_query` answers "how does this work",
the `runpod_*` tools answer "what do I actually have running".

## Refreshing

The converted tree feeds the docs content hash, so a changed upstream means
the docs corpus re-indexes on the next launch. To force a refresh, delete
`plugin-docs-src/runpod/` and restart.

## Provenance

Runpod's documentation is published at
[github.com/runpod/docs](https://github.com/runpod/docs) with no explicit
license grant. It is cloned locally for indexing and is not redistributed;
every converted page keeps a `source_url` pointing at the original.
