# Voitta Enterprise plugin — tool catalog

Active only on `enterprise.voitta.ai`.

## Status

Scaffolding only — no tools registered yet. This file will be
updated as tools are added.

## Adding a tool

1. Add a browser primitive in `frontend/widget.ts` — reads the DOM,
   calls the backend, or both.
2. Register a `ToolSpec` in `backend/voitta_enterprise/tools.py`.
3. Document it here (name, description, parameters, return shape,
   idiomatic call patterns).

See `plugins/ebay/` for a fully-worked example.
