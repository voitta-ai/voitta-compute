# Proposal: Git-based Script Versioning

## Problem

User-authored scripts in `scripts/` have no history. An `edit_script` call
overwrites the previous version with no recovery path. If a working report is
broken by a subsequent edit, the only recourse is to rewrite it from scratch.

---

## Options

### Option A — Single git repo at `scripts/`

One `git init` inside `USER_DATA_ROOT/scripts/`. Every slug is a subdirectory;
per-script log is `git log -- {slug}/`.

**Pros:**
- One repo to manage
- Atomic cross-script commits possible (rare but clean)
- `git log -- {slug}/` gives per-script history cheaply

**Cons:**
- `move_to_folder` must use `git mv` (not `shutil.move`) or history breaks
- `delete_script` must use `git rm -r` + commit
- Single lock — parallel writes (unlikely but possible) need care

---

### Option B — Per-script git repos

Each slug gets its own `git init` inside `{slug}/`.

**Pros:**
- Completely isolated — move, delete, rename don't affect other scripts
- No `git mv` complexity when foldering
- Trivial to expose as a zip/export per script

**Cons:**
- Many `git init` calls as scripts accumulate
- Can't do cross-script commits
- More bookkeeping to discover all repos

---

### Option C — Single repo at `USER_DATA_ROOT/` (scripts + python_storage)

Version everything — scripts and data snapshots — in one repo.

**Pros:**
- Single source of truth for all user artefacts

**Cons:**
- `python_storage/` can contain large binary files (video frames, images)
  → repo bloat, slow `git status`
- Almost no practical value in versioning opaque binary blobs
- Would need `.gitignore` inside to exclude `python_storage/` anyway —
  which is Option A

**Verdict: ruled out.**

---

### Option D — No git; append-only JSONL per script

On every write, append `{timestamp, code}` to `scripts/{slug}/history.jsonl`.

**Pros:**
- Zero dependency on git being installed
- Dead simple to implement and read back
- No `git mv` / `git rm` ceremony

**Cons:**
- Grows unbounded (needs manual pruning or capping)
- No diff tooling, no branching, no standard format
- Can't leverage git CLI the user already knows

---

## Recommendation: Option A (single repo) with graceful degradation

One repo at `USER_DATA_ROOT/scripts/`. Versioning is opt-in at runtime — if
`git` is not found on `PATH`, all write tools continue to work normally and
version tools return a clear `"git not available"` error rather than crashing.

### Changes required

**Backend — `store.py`**
- `_git(slug_dir, *args)` helper: runs `git -C SCRIPTS_DIR …`, returns
  `(ok, stdout)`. Catches `FileNotFoundError` for missing git.
- `_ensure_git_repo()`: `git init` if `.git/` absent; creates initial commit
  of any existing content.
- `write_script(slug, code, *, folder_name, message)`: after writing files,
  `git add {slug}/ && git commit -m {message}`. `message` defaults to
  `"define: {slug}"` or `"edit: {slug}"` if not provided.
- `delete_script(slug)`: `git rm -r {slug}/ && git commit -m "delete: {slug}"`
  instead of `shutil.rmtree`.
- `move_script_to_folder(slug, folder_name)`: `git mv {src} {dst} && git commit`.

**New tools**
- `list_script_versions(slug)` — `git log --oneline -- {slug}/`, returns
  `[{ref, message, timestamp}]`
- `get_script_version(slug, ref)` — `git show {ref}:{slug}/code.py`, returns
  source without restoring
- `restore_script_version(slug, ref)` — checkout code at ref, smoke-test,
  commit as `"restore: {slug} to {ref}"`. On smoke-test failure: revert,
  return error.

**Tool updates**
- `define_script` and `edit_script` gain optional `message: str` param.
- `system.md`: instruct LLM to always pass a short `message=` describing
  intent (one line, present tense: "add bar chart", "fix axis labels").

### Graceful degradation

```python
def _git(self, *args):
    try:
        r = subprocess.run(["git", "-C", str(SCRIPTS_DIR), *args], ...)
        return r.returncode == 0, r.stdout
    except FileNotFoundError:
        return False, "git not available"
```

All version tools check this flag and surface a readable message if git is
absent. Core write tools (`define_script`, `edit_script`) never fail because
of git.

### Migration (first run)

On first `write_script` call after the update:
1. `git init` if `.git/` absent
2. If scripts already exist: `git add -A && git commit -m "initial: import existing scripts"`
3. Proceed with the normal write + commit.

---

## What the LLM sees

Normal editing is unchanged. Recovery looks like:

```
User: "undo the last change to my-report"
LLM:  list_script_versions("my-report")
      → [{ref:"a3f1c", message:"edit: add bar chart", timestamp:"2026-05-25T14:32"}]
      restore_script_version("my-report", "a3f1c")
      → ok, script restored and re-run
```

---

## Effort estimate

- Backend changes: ~1 day
- New tools + tests: ~0.5 day
- `system.md` update: trivial
- Total: **~1.5 days**
