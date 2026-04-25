"""Provider auto-discovery + gating contracts.

Importing ``app.tools`` populates the global registry with the domain
tools and every provider's tools. Two gating contracts:

  • ``*_get_page_context`` (per-provider) is **host-gated** — it only
    appears on the provider's host (e.g. drive.google.com) because
    its output (folder/file/search the user is *currently looking at*)
    is meaningless elsewhere.
  • Action tools (list / search / download / export / …) are
    **visibility-gated** by an auth check, so they only appear once
    the user has connected the provider in Settings. They are NOT
    host-gated; the LLM can act on Drive content from any host page.
"""

from __future__ import annotations


def _registered():
    from app.tools import registry
    return registry


def test_drive_provider_registered():
    drive_tools = [s for s in _registered().all() if s.name.startswith("drive_")]
    assert drive_tools, (
        "no drive_* tools in registry — provider package failed to import; "
        "check app.tools.providers.drive.__init__"
    )


def test_drive_page_context_is_host_gated():
    by_name = {s.name: s for s in _registered().all()}
    assert "drive_get_page_context" in by_name
    spec = by_name["drive_get_page_context"]
    assert spec.host_pattern == "drive.google.com"


def test_drive_action_tools_are_visibility_gated_not_host_gated():
    by_name = {s.name: s for s in _registered().all()}
    for name in (
        "drive_list_files",
        "drive_search",
        "drive_get_file",
        "drive_download_to_python_storage",
        "drive_export_to_python_storage",
    ):
        spec = by_name.get(name)
        assert spec is not None, f"missing {name!r} in registry"
        assert spec.visibility_check is not None, (
            f"{name!r} must have visibility_check (e.g. google_oauth.is_connected)"
        )
        assert spec.host_pattern is None, (
            f"{name!r} should NOT be host-gated — action tools work "
            f"from any host page"
        )


def test_only_canonical_provider_tools_in_oss_tree():
    """Voitta core ships with the Google plugin only. Other tool
    families belong in private plugin overlays under /plugins/<name>/
    which are gitignored. This guards against accidental upstreaming
    of provider-specific tools."""
    names = _registered().names()
    forbidden = ("workitem", "force_com")
    for f in forbidden:
        assert not any(f in n.lower() for n in names), (
            f"forbidden tool family '{f}' present: "
            f"{[n for n in names if f in n.lower()]}"
        )


def test_generic_domain_tools_present():
    names = set(_registered().names())
    expected = {
        "rag_query",
        "web_fetch",
        "list_python_storage",
        "list_buffers",
        "buffer_eval",
    }
    missing = expected - names
    assert not missing, f"missing generic tools: {sorted(missing)}"
