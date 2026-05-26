from pathlib import Path

import pytest

from app.reports.slug import InvalidSlug, resolve_under, validate_slug


@pytest.mark.parametrize("good", ["a", "a-b", "a_b", "abc123", "x" * 64, "0", "report-2024"])
def test_validate_slug_accepts(good: str) -> None:
    assert validate_slug(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "",                # too short
        "X",               # uppercase
        "a/b",             # slash
        "a.b",             # dot
        "a b",             # space
        "x" * 65,          # too long
        "../etc/passwd",   # traversal
        "a$b",             # special
        "ä",               # unicode
    ],
)
def test_validate_slug_rejects(bad: str) -> None:
    with pytest.raises(InvalidSlug):
        validate_slug(bad)


def test_validate_slug_type_error_is_invalid_slug() -> None:
    with pytest.raises(InvalidSlug):
        validate_slug(None)  # type: ignore[arg-type]


def test_resolve_under_stays_inside_root(tmp_path: Path) -> None:
    target = resolve_under(tmp_path, "ok")
    assert target == (tmp_path / "ok").resolve()


def test_resolve_under_rejects_traversal_via_resolve(tmp_path: Path) -> None:
    # validate_slug catches this first, but make sure the resolve guard
    # would too if the regex ever loosens.
    with pytest.raises(InvalidSlug):
        resolve_under(tmp_path, "../escape")
