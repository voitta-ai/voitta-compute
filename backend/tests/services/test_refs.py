"""Tests for the path-based vre:// ref grammar in app.services.refs."""

import pytest
from app.services.refs import Ref, RefError, canonicalise, parse


# ---------------------------------------------------------------------------
# Happy-path parsing
# ---------------------------------------------------------------------------


def test_parse_simple():
    ref = parse("vre://Stella NFS/report.pdf")
    assert ref.scheme == "vre"
    assert ref.authority == "Stella NFS"
    assert ref.path == "report.pdf"
    assert ref.params == {}


def test_parse_nested_path():
    ref = parse("vre://Stella NFS/subdir/deep/file.pdf")
    assert ref.authority == "Stella NFS"
    assert ref.path == "subdir/deep/file.pdf"


def test_parse_with_asset_param():
    ref = parse("vre://Stella NFS/parts/frame.glb?asset=cad_mesh")
    assert ref.get("asset") == "cad_mesh"
    assert ref.path == "parts/frame.glb"


def test_parse_multiple_params():
    ref = parse("vre://Stella NFS/p.glb?asset=cad_projection&export=iso&slug=base/rail")
    assert ref.get("asset") == "cad_projection"
    assert ref.get("export") == "iso"
    assert ref.get("slug") == "base/rail"


def test_parse_default_asset():
    ref = parse("vre://Stella NFS/file.pdf")
    assert ref.get("asset", "original") == "original"


def test_parse_drive_scheme_unchanged():
    ref = parse("drive://1AbCXYZ")
    assert ref.scheme == "drive"
    assert ref.authority == "1AbCXYZ"
    assert ref.path == ""
    assert ref.params == {}


def test_parse_drive_with_export():
    ref = parse("drive://1AbCXYZ?export=text%2Fcsv")
    assert ref.get("export") == "text/csv"


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def test_canonical_param_order_independence():
    a = canonicalise("vre://Stella NFS/f.pdf?asset=original&slug=x")
    b = canonicalise("vre://Stella NFS/f.pdf?slug=x&asset=original")
    assert a == b


def test_canonical_is_stable():
    c = canonicalise("vre://Stella NFS/f.pdf?asset=cad_mesh")
    assert canonicalise(c) == c


def test_canonical_space_in_authority_roundtrips():
    ref = parse("vre://Stella NFS/file.pdf")
    # canonical encodes the space; re-parsing the canonical gives back same ref
    ref2 = parse(ref.canonical)
    assert ref2.authority == ref.authority
    assert ref2.path == ref.path


def test_canonical_no_params():
    c = canonicalise("vre://MyFolder/sub/file.pdf")
    assert c == "vre://MyFolder/sub/file.pdf"


def test_canonical_with_params_sorted():
    c = canonicalise("vre://F/f.pdf?z=1&a=2")
    # a comes before z
    assert c.index("a=") < c.index("z=")


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_parse_missing_scheme_separator():
    with pytest.raises(RefError):
        parse("vre:Stella NFS/file.pdf")


def test_parse_empty_authority():
    with pytest.raises(RefError):
        parse("vre:///file.pdf")


def test_parse_duplicate_param():
    with pytest.raises(RefError):
        parse("vre://F/f.pdf?a=1&a=2")


def test_parse_param_missing_equals():
    with pytest.raises(RefError):
        parse("vre://F/f.pdf?badparam")


def test_parse_empty_string():
    with pytest.raises(RefError):
        parse("")
