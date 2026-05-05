"""Config: prompt is provider-agnostic, no SIMR/UCP residue."""

from __future__ import annotations


def test_voitta_system_prompt_is_clean():
    from app.config import VOITTA_SYSTEM_PROMPT

    haystack = VOITTA_SYSTEM_PROMPT.lower()
    for forbidden in ("simr", "ucp", "datastudio", "salesforce", "force.com",
                      "dsuser", "dev-dp", "arcgis"):
        assert forbidden not in haystack, (
            f"system prompt still mentions {forbidden!r}"
        )


def test_voitta_brand_named():
    from app.config import VOITTA_SYSTEM_PROMPT

    assert "Voitta" in VOITTA_SYSTEM_PROMPT


def test_settings_namespace_has_prompt():
    from app.config import settings, VOITTA_SYSTEM_PROMPT

    assert settings.system_prompt == VOITTA_SYSTEM_PROMPT
    assert settings.host == "127.0.0.1"
    assert settings.port == 12358
