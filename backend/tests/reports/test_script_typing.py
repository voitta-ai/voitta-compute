"""Script typing: recording/dry-run Sheets client, kind inference,
sticky effects union, drift computation, the run_script confirm gate,
and legacy meta compatibility."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The sheets plugin is added to sys.path by the plugin loader at app
# startup; tests import it directly.
_SHEETS_BACKEND = (
    Path(__file__).resolve().parents[3] / "plugins" / "google" / "sheets" / "backend"
)
if str(_SHEETS_BACKEND) not in sys.path:
    sys.path.insert(0, str(_SHEETS_BACKEND))

from app.reports import script_typing
from app.reports.script_typing import (
    drift,
    infer_kind,
    mark_recently_edited,
    merge_effects,
    observed_effects,
    was_recently_edited,
)


# ---- recording client -------------------------------------------------------


class _FakeInner:
    """Stands in for SheetsClient; records which methods were hit."""

    def __init__(self):
        self.calls: list[str] = []

    def get(self, path, **p):
        self.calls.append(f"get:{path}")
        return {"values": [[1]]}

    def get_metadata(self, sid):
        self.calls.append(f"meta:{sid}")
        return {"sheets": []}

    def put(self, path, body=None, **p):
        self.calls.append(f"put:{path}")
        return {"updatedRange": "A1", "updatedRows": 1}

    def post(self, path, body=None, **p):
        self.calls.append(f"post:{path}")
        return {"replies": [{}]}


def _client(dry_run: bool):
    from voitta_sheets.client import RecordingSheetsClient

    inner = _FakeInner()
    effects: dict = {}
    return RecordingSheetsClient(inner, effects, dry_run), inner, effects


def test_reads_pass_through_and_record_nothing():
    c, inner, effects = _client(dry_run=True)
    assert c.get("sid/values/A1:B2")["values"] == [[1]]
    assert c.get_metadata("sid") == {"sheets": []}
    assert effects == {}
    assert inner.calls == ["get:sid/values/A1:B2", "meta:sid"]


def test_dry_run_write_is_stubbed_and_recorded():
    c, inner, effects = _client(dry_run=True)
    r = c.put("sid/values/Sheet1!A1", {"range": "Sheet1!A1", "values": [[1]]})
    assert r["dryRun"] is True
    assert r["updatedRange"] == "Sheet1!A1"       # shape-correct (issue A)
    assert effects["writes_external"] is True
    assert not any(x.startswith("put") for x in inner.calls)  # never hit Google


def test_dry_run_synth_shapes_for_batch_and_append():
    c, _inner, _e = _client(dry_run=True)
    assert c.post("sid:batchUpdate", {"requests": []})["replies"] == []
    r = c.post("sid/values/Sheet1!A1:append", {"range": "Sheet1!A1", "values": [[1]]})
    assert r["updates"]["updatedRange"] == "Sheet1!A1"
    assert r["updates"]["updatedRows"] == 0


def test_live_write_passes_through_and_records():
    c, inner, effects = _client(dry_run=False)
    r = c.put("sid/values/A1", {"range": "A1", "values": [[1]]})
    assert r == {"updatedRange": "A1", "updatedRows": 1}
    assert effects["writes_external"] is True
    assert inner.calls == ["put:sid/values/A1"]


# ---- inference / effects ----------------------------------------------------


def test_infer_kind_matrix():
    assert infer_kind("<html/>", 0) == "report"
    assert infer_kind(None, 2) == "chat"
    assert infer_kind(None, 0) == "job"
    assert infer_kind("<html/>", 2) == "report"    # HTML wins over inline


def test_sticky_union_conditional_writer_stays_gated():
    # Issue B scenario: run 1 wrote; run 2 (different args) didn't.
    after_run1 = merge_effects({}, {"writes_external": True, "renders_html": False})
    after_run2 = merge_effects(after_run1, {"renders_html": True})
    assert after_run2["writes_external"] is True   # gate never silently opens
    assert after_run2["renders_html"] is True


def test_merge_ignores_unknown_keys():
    merged = merge_effects({"bogus": True}, {"also_bogus": True, "emits_inline": True})
    assert merged == {"emits_inline": True}


class _Ctx:
    def __init__(self, inline=0, effects=None):
        self.inline = [object()] * inline
        self.effects = effects or {}


def test_observed_effects_combines_result_and_ctx():
    obs = observed_effects("<html/>", _Ctx(inline=1, effects={"writes_external": True}))
    assert obs == {
        "renders_html": True, "emits_inline": True, "writes_external": True,
    }


def test_drift_matrix():
    assert drift("chat", {"renders_html": True}) is not None
    assert drift("report", {"renders_html": True}) is None
    assert drift("report", {"writes_external": True}) is not None
    assert drift("job", {"writes_external": True}) is None      # jobs may write
    assert drift(None, {"writes_external": True}) is None       # unclassified: no drift
    assert drift("report", {"renders_html": True, "emits_inline": True}) is None


# ---- latch ------------------------------------------------------------------


def test_latch_roundtrip_and_isolation():
    script_typing._recent.clear()
    mark_recently_edited("sess-1", "my-script")
    assert was_recently_edited("sess-1", "my-script")
    assert not was_recently_edited("sess-2", "my-script")   # other session
    assert not was_recently_edited("sess-1", "other")       # other slug
    assert not was_recently_edited(None, "my-script")       # no session


def test_latch_expires(monkeypatch):
    script_typing._recent.clear()
    mark_recently_edited("s", "x")
    key = ("s", "x")
    script_typing._recent[key] -= script_typing._LATCH_TTL_S + 1
    assert not was_recently_edited("s", "x")


# ---- store meta round-trip --------------------------------------------------


def test_meta_kind_effects_roundtrip(tmp_path):
    from app.reports import store

    store.write_script("typed-script", "def build(ctx):\n    return None\n", root=tmp_path)
    # update via the root-less API is not available with root override;
    # exercise the serializer directly.
    m = store.read_meta("typed-script", root=tmp_path)
    m.kind = "job"
    m.effects = {"writes_external": True}
    store._write_meta("typed-script", m, root=tmp_path)
    back = store.read_meta("typed-script", root=tmp_path)
    assert back.kind == "job"
    assert back.effects == {"writes_external": True}
    # And they never leak into extra.
    assert "kind" not in back.extra and "effects" not in back.extra


def test_meta_legacy_file_reads_clean(tmp_path):
    from app.reports import store

    store.write_script("legacy", "def build(ctx):\n    return None\n", root=tmp_path)
    # Simulate a pre-typing meta.json (no kind/effects keys).
    import json
    d = tmp_path / "legacy"
    payload = json.loads((d / "meta.json").read_text())
    payload.pop("kind", None)
    payload.pop("effects", None)
    (d / "meta.json").write_text(json.dumps(payload))
    m = store.read_meta("legacy", root=tmp_path)
    assert m.kind is None
    assert m.effects == {}


# ---- confirm gate -----------------------------------------------------------


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    """Point the store at tmp_path via the paths indirection so the gate
    reads real meta. SCRIPTS_DIR is a UserPath; monkeypatch the resolver."""
    from app.reports import paths as rpaths

    monkeypatch.setattr(
        rpaths.SCRIPTS_DIR, "_resolver", lambda: tmp_path / "scripts", raising=False,
    )
    monkeypatch.setattr(
        rpaths.SCRIPTS_FOLDERS_DIR, "_resolver",
        lambda: tmp_path / "scripts" / "folders", raising=False,
    )
    return tmp_path


def test_gate_on_recorded_writer(scripted):
    from app.reports import store
    from app.tools.registry import ToolCtx
    from app.tools.server.scripts.run_script import _confirm_gate

    script_typing._recent.clear()
    store.write_script("writer", "def build(ctx):\n    return None\n")
    store.update_meta("writer", kind="job", effects={"writes_external": True})
    msg = _confirm_gate("writer", ToolCtx(session_id="s1"))
    assert msg is not None and "Google Sheets" in msg


def test_gate_skipped_after_same_session_edit(scripted):
    from app.reports import store
    from app.tools.registry import ToolCtx
    from app.tools.server.scripts.run_script import _confirm_gate

    script_typing._recent.clear()
    store.write_script("writer2", "def build(ctx):\n    return None\n")
    store.update_meta("writer2", kind="job", effects={"writes_external": True})
    mark_recently_edited("s1", "writer2")
    assert _confirm_gate("writer2", ToolCtx(session_id="s1")) is None
    # …but another session still gets gated.
    assert _confirm_gate("writer2", ToolCtx(session_id="s2")) is not None


def test_gate_legacy_static_check(scripted):
    from app.reports import store
    from app.tools.registry import ToolCtx
    from app.tools.server.scripts.run_script import _confirm_gate

    script_typing._recent.clear()
    # Unclassified + mentions ctx.sheets → gated (D3).
    store.write_script(
        "legacy-sheets",
        "def build(ctx):\n    ctx.sheets.get('sid')\n    return None\n",
    )
    assert _confirm_gate("legacy-sheets", ToolCtx(session_id="s")) is not None
    # Unclassified, no ctx.sheets → ungated.
    store.write_script("legacy-plain", "def build(ctx):\n    return None\n")
    assert _confirm_gate("legacy-plain", ToolCtx(session_id="s")) is None
    # Classified read-only sheets script → ungated.
    store.write_script(
        "reader", "def build(ctx):\n    ctx.sheets.get('sid')\n    return None\n",
    )
    store.update_meta("reader", kind="chat", effects={"emits_inline": True})
    assert _confirm_gate("reader", ToolCtx(session_id="s")) is None
