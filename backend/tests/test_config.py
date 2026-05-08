"""Config: system prompt is provider-agnostic.

Voitta core's system prompt should be brand-only — no third-party host
or product names. Provider-specific guidance is added per-plugin (the
plugin's docs land in the RAG corpus and surface via ``rag_query`` when
the user is on a relevant host). This test pins that boundary so a
careless edit doesn't bake provider knowledge into core.
"""

from __future__ import annotations


def test_voitta_system_prompt_is_clean():
    from app.config import VOITTA_SYSTEM_PROMPT

    haystack = VOITTA_SYSTEM_PROMPT.lower()
    # Hostnames + product names that historically leaked from forks of
    # this repo. If you legitimately need to mention a provider in core,
    # update this list AND the docs/13-plugins.md note about boundaries.
    forbidden = ("force.com", "datastudio")
    for f in forbidden:
        assert f not in haystack, (
            f"system prompt still mentions {f!r}"
        )


def test_voitta_brand_named():
    from app.config import VOITTA_SYSTEM_PROMPT

    assert "Voitta" in VOITTA_SYSTEM_PROMPT


def test_settings_namespace_has_prompt():
    from app.config import settings, VOITTA_SYSTEM_PROMPT

    assert settings.system_prompt == VOITTA_SYSTEM_PROMPT
    assert settings.host == "127.0.0.1"
    assert settings.port == 12358
