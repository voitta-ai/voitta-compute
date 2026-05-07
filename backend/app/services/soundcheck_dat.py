"""Parse SoundCheck (Listen, Inc.) ``.dat`` / ``.wfm`` / ``.res`` binary
files into the curves contract used by the rest of the backend.

Authoritative spec: SoundCheck 21 Manual, chapter 35 "Data File Format".

  * §35.1 — DAT v2  (SoundCheck 4.13)
  * §35.2 — DAT v3  (SoundCheck 5.54)
  * §35.3 — DAT v6  (SoundCheck 6.01–7.01+)
  * §35.4 — WFM v3  (SoundCheck 6.01–7.01+)
  * §35.5 — Res     (Result file)

LabVIEW flattened-data conventions apply: big-endian, IEEE 754 doubles,
32-bit length-prefixed ASCII strings without termination. The SC 6.11
manual's DAT layout omits the ``Fill Baseline`` field that SC ≥ 14 added,
so v6 has two on-disk variants — both are accepted via a roll-back probe
once the trailing fields are entered.

Public surface used by the ``dat_parse`` compute script and the
``dat_curves`` report:

  * :func:`find_dat_in_dir`        — locate the .dat in a snapshot dir
  * :func:`parse_dat_file`         — low-level parse → list of records
  * :func:`dat_to_curves_body`     — convert to the canonical
                                      ``{"curves":[…]}`` shape
  * :func:`parse_dat_to_snapshot`  — main entry; writes
                                      ``dat_summary.json`` + ``curves.pkl``
                                      into a snapshot directory.

Pure module — no FastAPI / Panel imports, no global state.
"""

from __future__ import annotations

import glob
import json
import os
import struct
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


LogFn = Callable[[str], None]


# --------------------------------------------------------------------- io --


class _Reader:
    __slots__ = ("buf", "pos", "limit")

    def __init__(self, buf: bytes, pos: int = 0, limit: int | None = None) -> None:
        self.buf = buf
        self.pos = pos
        self.limit = limit if limit is not None else len(buf)

    def remaining(self) -> int:
        return self.limit - self.pos

    def _need(self, n: int) -> bool:
        return self.remaining() >= n

    def u8(self) -> int:
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        (v,) = struct.unpack_from(">H", self.buf, self.pos)
        self.pos += 2
        return v

    def i16(self) -> int:
        (v,) = struct.unpack_from(">h", self.buf, self.pos)
        self.pos += 2
        return v

    def u32(self) -> int:
        (v,) = struct.unpack_from(">I", self.buf, self.pos)
        self.pos += 4
        return v

    def i32(self) -> int:
        (v,) = struct.unpack_from(">i", self.buf, self.pos)
        self.pos += 4
        return v

    def f64(self) -> float:
        (v,) = struct.unpack_from(">d", self.buf, self.pos)
        self.pos += 8
        return v

    def doubles(self, n: int) -> list[float]:
        vals = list(struct.unpack_from(f">{n}d", self.buf, self.pos))
        self.pos += 8 * n
        return vals

    def floats(self, n: int) -> list[float]:
        vals = list(struct.unpack_from(f">{n}f", self.buf, self.pos))
        self.pos += 4 * n
        return vals

    def fixed(self, n: int) -> bytes:
        v = self.buf[self.pos : self.pos + n]
        self.pos += n
        return v

    def lv_string(self) -> str:
        n = self.u32()
        s = self.buf[self.pos : self.pos + n]
        self.pos += n
        return s.decode("ascii", errors="replace")

    def bool1(self) -> bool:
        v = self.buf[self.pos]
        self.pos += 1
        return v != 0

    def opt_u32(self) -> int | None:
        return self.u32() if self._need(4) else None

    def opt_u8(self) -> int | None:
        return self.u8() if self._need(1) else None

    def opt_bool(self) -> bool | None:
        return self.bool1() if self._need(1) else None


# ----------------------------------------------------------------- header --


@dataclass
class _Header:
    cluster_type: str
    cluster_version: int
    n_dims: int
    fixed_name: str
    reserved_hex: str


def _read_header(r: _Reader) -> _Header:
    raw_type = r.fixed(16).decode("ascii", errors="replace")
    cluster_version = r.u16()
    n_dims = r.u8()
    fixed_name_bytes = r.fixed(42)
    reserved = r.fixed(3)
    return _Header(
        cluster_type=raw_type,
        cluster_version=cluster_version,
        n_dims=n_dims,
        fixed_name=fixed_name_bytes.decode("ascii", errors="replace").rstrip(" \x00"),
        reserved_hex=reserved.hex(),
    )


# -------------------------------------------------------------- DAT curve --


@dataclass
class DataCurve:
    """One ``Data`` cluster from a SoundCheck .dat file. Field availability
    depends on the DAT version (None when the version doesn't carry that
    field). See :mod:`backend.app.services.soundcheck_dat` for layout."""

    index: int
    offset: int
    struct_bytes: int
    header: _Header
    name: str
    n_points: int
    points: list[tuple[float, float, float]]
    x_data_kind: int
    y_data_kind: int
    z_data_kind: int
    x_axis_kind: int
    y_axis_kind: int
    z_axis_kind: int
    x_prefix: str | None = None
    y_prefix: str | None = None
    z_prefix: str | None = None
    x_unit: str = ""
    y_unit: str = ""
    z_unit: str = ""
    x_db_ref: float = 0.0
    y_db_ref: float = 0.0
    z_db_ref: float = 0.0
    single_value: bool = False
    protected: bool | None = None
    display_x: bool | None = None
    display_y: bool | None = None
    display_z: bool | None = None
    plot_color_rgba: int | None = None
    plot_interp: int | None = None
    plot_point_style: int | None = None
    plot_line_style: int | None = None
    plot_point_color_rgba: int | None = None
    plot_line_width: int | None = None
    plot_bar_style: int | None = None
    fill_baseline: int | None = None
    test_info_n: int | None = None
    test_info_raw_hex: str | None = None
    extra_hex: str = ""
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class Waveform:
    """One ``Waveform`` cluster from a .wfm file (DAT chapter §35.4)."""

    index: int
    offset: int
    struct_bytes: int
    header: _Header
    name: str
    x0: float
    dx: float
    n_points: int
    points: list[float]
    x_data_kind: int
    y_data_kind: int
    y_axis_kind: int
    x_unit: str
    y_unit: str
    y_db_ref: float
    display_y: bool
    display_x: bool
    overload: bool
    protected: bool
    sequence_history: list[str]
    channel_number: int
    plot_color_rgba: int
    plot_interp: int
    plot_point_style: int
    plot_line_style: int
    plot_point_color_rgba: int
    plot_line_width: int
    plot_bar_style: int
    fill_baseline: int | None = None
    test_info_n: int | None = None
    test_info_raw_hex: str | None = None
    extra_hex: str = ""


@dataclass
class Result:
    """One ``Result`` cluster from a .res file (§35.5)."""

    index: int
    offset: int
    struct_bytes: int
    header: _Header
    name: str
    margin: float
    unit: str
    verdict: bool
    limit: str
    max_min: str
    protected: bool
    test_info: str
    unit_type: int
    extra_hex: str = ""


def _parse_data_curve(buf: bytes, item_start: int, index: int) -> DataCurve:
    r = _Reader(buf, item_start)
    struct_bytes = r.u32()
    body_start = r.pos
    body_end = body_start + struct_bytes
    r = _Reader(buf, body_start, body_end)

    hdr = _read_header(r)
    if hdr.cluster_type.rstrip() != "Data":
        raise ValueError(f"Expected 'Data' cluster, got {hdr.cluster_type!r}")
    v = hdr.cluster_version

    name = r.lv_string()
    n_points = r.u32()
    flat = r.doubles(n_points * 3)
    points = [tuple(flat[i : i + 3]) for i in range(0, len(flat), 3)]

    x_data_kind, y_data_kind, z_data_kind = r.u16(), r.u16(), r.u16()
    x_axis_kind, y_axis_kind, z_axis_kind = r.u16(), r.u16(), r.u16()

    curve = DataCurve(
        index=index,
        offset=item_start,
        struct_bytes=struct_bytes,
        header=hdr,
        name=name,
        n_points=n_points,
        points=points,
        x_data_kind=x_data_kind,
        y_data_kind=y_data_kind,
        z_data_kind=z_data_kind,
        x_axis_kind=x_axis_kind,
        y_axis_kind=y_axis_kind,
        z_axis_kind=z_axis_kind,
    )

    if v in (2, 3):
        curve.x_prefix = r.lv_string()
        curve.y_prefix = r.lv_string()
        curve.z_prefix = r.lv_string()

    curve.x_unit = r.lv_string()
    curve.y_unit = r.lv_string()
    curve.z_unit = r.lv_string()
    curve.x_db_ref = r.f64()
    curve.y_db_ref = r.f64()
    curve.z_db_ref = r.f64()
    curve.single_value = r.bool1()

    if v >= 3:
        curve.protected = r.opt_bool()
        curve.display_x = r.opt_bool()
        curve.display_y = r.opt_bool()
        curve.display_z = r.opt_bool()
        curve.plot_color_rgba = r.opt_u32()

    if v >= 6:
        curve.plot_interp = r.opt_u8()
        curve.plot_point_style = r.opt_u8()
        curve.plot_line_style = r.opt_u8()
        curve.plot_point_color_rgba = r.opt_u32()
        curve.plot_line_width = r.opt_u8()
        curve.plot_bar_style = r.opt_u8()
        # SC ≥ 14 adds Fill Baseline (i16) before Test Info; SC 6.11 omits
        # it. Probe: peek `i16 + u32`, then ensure the u32 fits — if not,
        # roll back and read u32 directly as the Test Info length.
        if r.remaining() >= 6:
            saved = r.pos
            tentative_baseline = r.i16()
            tentative_n = r.u32()
            if tentative_n <= r.remaining():
                curve.fill_baseline = tentative_baseline
                curve.test_info_n = tentative_n
                curve.test_info_raw_hex = r.fixed(tentative_n).hex()
            else:
                r.pos = saved
                curve.test_info_n = r.opt_u32()
                if curve.test_info_n is not None:
                    curve.test_info_raw_hex = r.fixed(curve.test_info_n).hex()
        else:
            curve.test_info_n = r.opt_u32()
            if curve.test_info_n is not None:
                curve.test_info_raw_hex = r.fixed(curve.test_info_n).hex()

    if v not in (2, 3, 6):
        curve.parse_warnings.append(
            f"unknown cluster_version={v}; parsed best-effort using v6 layout"
        )

    if r.remaining() > 0:
        curve.extra_hex = buf[r.pos : body_end].hex()

    return curve


def _parse_waveform(buf: bytes, item_start: int, index: int) -> Waveform:
    r = _Reader(buf, item_start)
    struct_bytes = r.u32()
    body_start = r.pos
    body_end = body_start + struct_bytes
    r = _Reader(buf, body_start, body_end)

    hdr = _read_header(r)
    if hdr.cluster_type.rstrip() != "Waveform":
        raise ValueError(f"Expected 'Waveform' cluster, got {hdr.cluster_type!r}")

    name = r.lv_string()
    x0 = r.f64()
    dx = r.f64()
    n_points = r.u32()
    points = r.floats(n_points)

    wfm = Waveform(
        index=index,
        offset=item_start,
        struct_bytes=struct_bytes,
        header=hdr,
        name=name,
        x0=x0,
        dx=dx,
        n_points=n_points,
        points=points,
        x_data_kind=r.u16(),
        y_data_kind=r.u16(),
        y_axis_kind=r.u16(),
        x_unit=r.lv_string(),
        y_unit=r.lv_string(),
        y_db_ref=r.f64(),
        display_y=r.bool1(),
        display_x=r.bool1(),
        overload=r.bool1(),
        protected=r.bool1(),
        sequence_history=[],
        channel_number=0,
        plot_color_rgba=0,
        plot_interp=0,
        plot_point_style=0,
        plot_line_style=0,
        plot_point_color_rgba=0,
        plot_line_width=0,
        plot_bar_style=0,
    )
    n_steps = r.u32()
    wfm.sequence_history = [r.lv_string() for _ in range(n_steps)]
    wfm.channel_number = r.i32()
    wfm.plot_color_rgba = r.u32()
    wfm.plot_interp = r.u8()
    wfm.plot_point_style = r.u8()
    wfm.plot_line_style = r.u8()
    wfm.plot_point_color_rgba = r.u32()
    wfm.plot_line_width = r.u8()
    wfm.plot_bar_style = r.u8()

    if r.remaining() >= 6:
        saved = r.pos
        baseline = r.i16()
        tn = r.u32()
        if tn <= r.remaining():
            wfm.fill_baseline = baseline
            wfm.test_info_n = tn
            wfm.test_info_raw_hex = r.fixed(tn).hex()
        else:
            r.pos = saved
            wfm.test_info_n = r.opt_u32()
            if wfm.test_info_n is not None:
                wfm.test_info_raw_hex = r.fixed(wfm.test_info_n).hex()
    else:
        wfm.test_info_n = r.opt_u32()
        if wfm.test_info_n is not None:
            wfm.test_info_raw_hex = r.fixed(wfm.test_info_n).hex()

    if r.remaining() > 0:
        wfm.extra_hex = buf[r.pos : body_end].hex()
    return wfm


def _parse_result(buf: bytes, item_start: int, index: int) -> Result:
    r = _Reader(buf, item_start)
    struct_bytes = r.u32()
    body_start = r.pos
    body_end = body_start + struct_bytes
    r = _Reader(buf, body_start, body_end)

    hdr = _read_header(r)
    if hdr.cluster_type.rstrip() != "Result":
        raise ValueError(f"Expected 'Result' cluster, got {hdr.cluster_type!r}")

    res = Result(
        index=index,
        offset=item_start,
        struct_bytes=struct_bytes,
        header=hdr,
        name=r.lv_string(),
        margin=r.f64(),
        unit=r.lv_string(),
        verdict=r.bool1(),
        limit=r.lv_string(),
        max_min=r.lv_string(),
        protected=r.bool1(),
        test_info=r.lv_string(),
        unit_type=r.u16(),
    )
    if r.remaining() > 0:
        res.extra_hex = buf[r.pos : body_end].hex()
    return res


# ------------------------------------------------------------ public api ---


def find_dat_in_dir(dir_path: str) -> str:
    """Locate a SoundCheck binary inside a snapshot directory.

    Picks the first match of ``*.dat`` / ``*.wfm`` / ``*.res``. Raises
    ``RuntimeError`` if none is present or if the file's leading 16-byte
    cluster tag isn't one of the documented ones (cheap sanity check that
    catches drag-and-drops of unrelated ``.dat`` blobs)."""
    candidates: list[str] = []
    for ext in ("dat", "DAT", "wfm", "WFM", "res", "RES"):
        candidates += sorted(glob.glob(os.path.join(dir_path, f"*.{ext}")))
    if not candidates:
        raise RuntimeError(
            f"No .dat / .wfm / .res file in {dir_path}. Upload one first."
        )
    path = candidates[0]
    with open(path, "rb") as f:
        head = f.read(8 + 16 + 16)
    if len(head) < 24:
        raise RuntimeError(f"{os.path.basename(path)} is too small to be a SoundCheck file.")
    # Cluster tag is at byte 8 (after the file's u32 count and the item's
    # u32 size). Check the first 16 bytes there are one of the documented
    # space-padded tags.
    tag = head[8:24].decode("ascii", errors="replace").rstrip()
    if tag not in ("Data", "Waveform", "Result"):
        raise RuntimeError(
            f"{os.path.basename(path)}: header cluster tag {tag!r} is not "
            "'Data', 'Waveform', or 'Result' — not a SoundCheck binary file."
        )
    return path


def parse_dat_file(path: str) -> dict[str, Any]:
    """Parse a SoundCheck binary file and return ``{kind, n_items, items, leftover_bytes}``.

    ``kind`` is ``"DAT"`` / ``"WFM"`` / ``"RES"``. ``items`` is a list of
    :class:`DataCurve`, :class:`Waveform`, or :class:`Result`. The parser
    is strict: any per-item layout mismatch raises; trailing bytes inside
    a single item's declared size are captured in ``extra_hex`` rather
    than failing.
    """
    with open(path, "rb") as f:
        buf = f.read()
    n_items = struct.unpack_from(">I", buf, 0)[0]
    pos = 4
    if n_items == 0 or len(buf) < 4 + 4 + 16:
        return {"path": path, "kind": None, "n_items": 0, "items": [], "leftover_bytes": len(buf) - pos}

    first_tag = buf[pos + 4 : pos + 4 + 16].decode("ascii", errors="replace").rstrip()
    if first_tag == "Data":
        parser, kind = _parse_data_curve, "DAT"
    elif first_tag == "Waveform":
        parser, kind = _parse_waveform, "WFM"
    elif first_tag == "Result":
        parser, kind = _parse_result, "RES"
    else:
        raise ValueError(f"Unknown SoundCheck cluster tag: {first_tag!r}")

    items: list[Any] = []
    for i in range(n_items):
        struct_bytes = struct.unpack_from(">I", buf, pos)[0]
        items.append(parser(buf, pos, i))
        pos += 4 + struct_bytes

    return {
        "path": path,
        "kind": kind,
        "n_items": n_items,
        "items": items,
        "leftover_bytes": len(buf) - pos,
    }


def _curve_kind_label(name: str) -> str:
    """First whitespace-delimited token, with ``"Total Distortion"`` kept whole."""
    if name.startswith("Total Distortion"):
        return "Total Distortion"
    return name.split(" ", 1)[0] if " " in name else name


def _split_curve_meta(name: str) -> dict[str, str]:
    """Pull ``s/n`` and ``Time`` out of a SoundCheck curve title.

    Titles look like ``"PFR  s/n: 15  Time: 2023/1/11 9:42:39"``. We
    split on the canonical key tokens so values that contain spaces
    (the timestamp) round-trip intact.
    """
    out: dict[str, str] = {"kind": _curve_kind_label(name)}
    rest = name
    for key in ("s/n:", "Time:"):
        idx = rest.find(key)
        if idx < 0:
            continue
        # Everything from `key` to the next two-space gap is this value.
        tail = rest[idx + len(key) :].lstrip()
        # Fields are separated by ≥ 2 spaces; for the last field, trail to EOL.
        end = tail.find("  ")
        value = (tail if end < 0 else tail[:end]).strip()
        out[key.rstrip(":")] = value
    return out


def dat_to_curves_body(parsed: dict[str, Any]) -> dict[str, Any]:
    """Convert a :func:`parse_dat_file` result to the canonical
    ``{"curves":[…]}`` body that ``python_storage._flatten_and_pickle``
    accepts. Only DAT files map cleanly; WFM and RES are returned with
    a stripped-down shape.
    """
    kind = parsed["kind"]
    items = parsed["items"]
    if kind == "DAT":
        curves = []
        for c in items:
            meta = _split_curve_meta(c.name)
            x_vals = [p[0] for p in c.points]
            y_vals = [p[1] for p in c.points]
            z_vals = [p[2] for p in c.points]
            curves.append({
                "id": c.index,
                "name": c.name,
                "metadata": [
                    *([{"key": "kind", "value": meta["kind"]}] if "kind" in meta else []),
                    *([{"key": "s/n", "value": meta["s/n"]}] if "s/n" in meta else []),
                    *([{"key": "Time", "value": meta["Time"]}] if "Time" in meta else []),
                    {"key": "x_db_ref", "value": c.x_db_ref},
                    {"key": "y_db_ref", "value": c.y_db_ref},
                    {"key": "z_db_ref", "value": c.z_db_ref},
                    {"key": "cluster_version", "value": c.header.cluster_version},
                ],
                "series": [
                    {"name": "X", "unit": c.x_unit, "values": x_vals},
                    {"name": "Y", "unit": c.y_unit, "values": y_vals},
                    {"name": "Z", "unit": c.z_unit, "values": z_vals},
                ],
            })
        return {
            "filename": os.path.basename(parsed["path"]),
            "datasetName": "SoundCheck DAT",
            "parsedWith": "soundcheck_dat (DAT v6 + v2/v3 fallback)",
            "curves": curves,
        }
    if kind == "WFM":
        curves = []
        for w in items:
            curves.append({
                "id": w.index,
                "name": w.name,
                "metadata": [
                    {"key": "kind", "value": "Waveform"},
                    {"key": "x0", "value": w.x0},
                    {"key": "dx", "value": w.dx},
                    {"key": "channel", "value": w.channel_number},
                    {"key": "y_db_ref", "value": w.y_db_ref},
                    {"key": "cluster_version", "value": w.header.cluster_version},
                ],
                "series": [
                    {"name": "Y", "unit": w.y_unit, "values": w.points},
                ],
            })
        return {
            "filename": os.path.basename(parsed["path"]),
            "datasetName": "SoundCheck WFM",
            "parsedWith": "soundcheck_dat (WFM v3)",
            "curves": curves,
        }
    if kind == "RES":
        # Each Result is a scalar; keep them as one "curve" each with a
        # single-value Y series so the flatten path produces sensible rows.
        curves = []
        for rec in items:
            curves.append({
                "id": rec.index,
                "name": rec.name,
                "metadata": [
                    {"key": "kind", "value": "Result"},
                    {"key": "verdict", "value": rec.verdict},
                    {"key": "limit", "value": rec.limit},
                    {"key": "max_min", "value": rec.max_min},
                    {"key": "unit_type", "value": rec.unit_type},
                ],
                "series": [
                    {"name": "Y", "unit": rec.unit, "values": [rec.margin]},
                ],
            })
        return {
            "filename": os.path.basename(parsed["path"]),
            "datasetName": "SoundCheck RES",
            "parsedWith": "soundcheck_dat (Result)",
            "curves": curves,
        }
    return {
        "filename": os.path.basename(parsed["path"]),
        "datasetName": f"SoundCheck {kind}",
        "parsedWith": "soundcheck_dat",
        "curves": [],
    }


def _summary_from_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    items = parsed["items"]
    kind = parsed["kind"]
    if kind == "DAT":
        kinds: dict[str, int] = {}
        units: set[tuple[str, str, str]] = set()
        serials: set[str] = set()
        versions: set[int] = set()
        for c in items:
            kinds[_curve_kind_label(c.name)] = kinds.get(_curve_kind_label(c.name), 0) + 1
            units.add((c.x_unit, c.y_unit, c.z_unit))
            versions.add(c.header.cluster_version)
            meta = _split_curve_meta(c.name)
            if "s/n" in meta:
                serials.add(meta["s/n"])
        first = items[0] if items else None
        return {
            "kind": "DAT",
            "n_items": parsed["n_items"],
            "cluster_versions": sorted(versions),
            "curve_kinds": kinds,
            "n_unique_serials": len(serials),
            "unique_unit_triples": sorted(units),
            "all_clean_parses": all(c.extra_hex == "" for c in items),
            "leftover_bytes": parsed["leftover_bytes"],
            "first_curve_name": (first.name if first else None),
        }
    if kind == "WFM":
        return {
            "kind": "WFM",
            "n_items": parsed["n_items"],
            "cluster_versions": sorted({w.header.cluster_version for w in items}),
            "all_clean_parses": all(w.extra_hex == "" for w in items),
            "leftover_bytes": parsed["leftover_bytes"],
        }
    if kind == "RES":
        return {
            "kind": "RES",
            "n_items": parsed["n_items"],
            "verdicts_pass": sum(1 for r in items if r.verdict),
            "verdicts_fail": sum(1 for r in items if not r.verdict),
            "leftover_bytes": parsed["leftover_bytes"],
        }
    return {"kind": kind, "n_items": parsed["n_items"]}


def parse_dat_to_snapshot(
    dat_path: str,
    out_dir: str,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Parse ``dat_path`` and write the curves contract into ``out_dir``.

    Outputs (mirrors how ``python_storage`` lays out a curves snapshot,
    so the rest of the system — ``ctx.dataframe(handle)``, the
    ``dat_curves`` report, generic chart tooling — sees no surprises):

      * ``dat_summary.json``   — parser metadata + curve-kind counts
      * ``dat_curves.json``    — full curves body (X/Y/Z values per curve)
      * ``curves.pkl``         — long-form pandas DataFrame, one row per
                                  (curve, series, value)

    Returns the summary dict plus written-file metadata.
    """
    _log = log or (lambda _msg: None)
    t0 = time.time()
    size_mb = os.path.getsize(dat_path) / 1e6
    _log(f"file: {os.path.basename(dat_path)} ({size_mb:.2f} MB)")

    parsed = parse_dat_file(dat_path)
    _log(f"parsed {parsed['n_items']} items ({parsed['kind']}) in {time.time() - t0:.2f}s")

    body = dat_to_curves_body(parsed)
    summary = _summary_from_parsed(parsed)

    written: list[dict[str, Any]] = []

    summary_path = os.path.join(out_dir, "dat_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    written.append({"name": "dat_summary.json", "bytes": os.path.getsize(summary_path)})

    body_path = os.path.join(out_dir, "dat_curves.json")
    with open(body_path, "w") as f:
        json.dump(body, f, ensure_ascii=False, default=str)
    written.append({"name": "dat_curves.json", "bytes": os.path.getsize(body_path)})

    # Long-form DataFrame for ctx.dataframe(handle). Best-effort — pandas
    # is a heavy dep, so pickle is gated by import success. The rest of
    # the pipeline still works without it (the report reads dat_curves.json
    # directly).
    try:
        import pandas as pd  # noqa: F401

        rows: list[dict[str, Any]] = []
        for ci, c in enumerate(body["curves"]):
            meta_dict = {f"meta.{m['key']}": m.get("value") for m in c.get("metadata") or []}
            for s in c.get("series") or []:
                values = s.get("values") or []
                for vi, v in enumerate(values):
                    rows.append({
                        "curve_idx": ci,
                        "curve_id": c.get("id"),
                        "curve_name": c.get("name"),
                        "series_name": s.get("name"),
                        "series_unit": s.get("unit"),
                        "value_idx": vi,
                        "value": v,
                        **meta_dict,
                    })
        if rows:
            df = pd.DataFrame(rows)
            pkl_path = os.path.join(out_dir, "curves.pkl")
            df.to_pickle(pkl_path)
            written.append({
                "name": "curves.pkl",
                "bytes": os.path.getsize(pkl_path),
                "row_count": int(len(df)),
                "columns": list(df.columns),
            })
            _log(f"wrote curves.pkl: {len(df):,} rows")
    except ImportError:
        _log("pandas not available — skipped curves.pkl")

    summary["files_written"] = written
    summary["elapsed_s"] = round(time.time() - t0, 3)
    summary["dat_path"] = dat_path
    return summary
