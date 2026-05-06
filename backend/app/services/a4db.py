"""Parse Altair Animator ``.a4db`` files into the viewer-asset contract
(``geometry_grouped.json`` + ``parts_grouped.json``) consumed by the
``yaris_3d`` Three.js report.

``.a4db`` is HDF5 underneath, despite the proprietary extension. Schema
verified on a 1.6 GB BMW LS-DYNA crash export::

    /model_0/geometry_0/nodes_0/grids/
        ids                      (N,)
        coordinates/{x,y,z}      (N,) float32
    /model_0/geometry_0/elements_0/{quads,hexas,beams}/
        ids                      (E,)            element ids in topology order
        topologies               (E*K,) int32    K=4 quads, 8 hexas, 2 beams
    /model_0/geometry_0/properties_0/{quads,hexas,beams}/<name>/
        attrs: title, id (PID)
        <name>: (M,) int32       0-based topology indices owned by this property
    /model_0/results_0/step_*/{displacements/{x,y,z}, function_*/...}

Output schema (matches what ``scripts/reports/yaris_3d/code.py`` reads)::

    geometry_grouped.json   {interior: [flat], groups: {title: [flat]}}
    parts_grouped.json      {title: [{id, type, elements}]}

Coordinates are normalised to a unit cube and Y-up swapped (FEM Z → Three Y,
FEM Y → -Three Z) so the existing viewer renders without changes.

FEMZIP-A4DB (``.a4db.fz``) is detected by magic bytes and rejected with
a clear error — decompression needs SIDACT's proprietary library, which
this codebase does not bundle.
"""

from __future__ import annotations

import glob
import json
import os
import time
from typing import Any, Callable

import h5py
import numpy as np


HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
FEMZIP_MAGIC = b"\x19\x4d\x95\x04"

# Output budget — two iframe limits compete here (see
# docs/09-panel-threejs-reports.md):
#   • Static JSON: ~25 MB hard wall. Each edge ≈ 70 B → ~350K edges.
#   • Animated base64 binary: ~10–15 MB encoded. With ~31 timesteps the
#     limit is ~M·12·T ≤ ~10 MB raw, i.e. ~25 K active vertices per frame.
# Active vertex count is the dominant constraint for animation; keep
# the per-group cap tight enough that all 324 property titles still
# render and animation stays smooth (≥8 frames).
DEFAULT_MAX_INTERIOR_EDGES = 15_000
DEFAULT_MAX_HEX_EDGES_PER_GROUP = 6_000
DEFAULT_MAX_QUAD_EDGES_PER_GROUP = 6_000

# Iframe parse risk threshold for the warning emitted in the result.
IFRAME_JSON_LIMIT_MB = 25.0

LogFn = Callable[[str], None]


def find_a4db_in_dir(dir_path: str) -> str:
    """Locate the .a4db file inside a snapshot directory.

    Raises ``RuntimeError`` with an actionable message when the file is
    missing, FEMZIP-compressed, or not actually HDF5.
    """
    a4dbs = sorted(glob.glob(os.path.join(dir_path, "*.a4db")))
    if not a4dbs:
        fzs = (
            glob.glob(os.path.join(dir_path, "*.fz"))
            + glob.glob(os.path.join(dir_path, "*.a4db.fz"))
        )
        if fzs:
            raise RuntimeError(
                f"Found FEMZIP-compressed file {os.path.basename(fzs[0])}. "
                "FEMZIP-A4DB is SIDACT's proprietary compression; this parser "
                "only handles plain (uncompressed) .a4db. Decompress externally "
                "first (Animator File>Save As, or SIDACT femunzip) and re-upload."
            )
        raise RuntimeError(f"No .a4db file in {dir_path}")

    path = a4dbs[0]
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(FEMZIP_MAGIC):
        raise RuntimeError(
            f"{os.path.basename(path)} has .a4db extension but FEMZIP magic — "
            "decompress with SIDACT femunzip first."
        )
    if not head.startswith(HDF5_MAGIC):
        raise RuntimeError(
            f"{os.path.basename(path)} is not HDF5 (magic={head[:8].hex()})"
        )
    return path


def parse_a4db_to_viewer_assets(
    a4db_path: str,
    out_dir: str,
    *,
    max_interior_edges: int = DEFAULT_MAX_INTERIOR_EDGES,
    max_hex_edges_per_group: int = DEFAULT_MAX_HEX_EDGES_PER_GROUP,
    max_quad_edges_per_group: int = DEFAULT_MAX_QUAD_EDGES_PER_GROUP,
    include_animation: bool = True,
    frame_stride: int = 1,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Parse ``a4db_path`` (plain HDF5) and write viewer assets to
    ``out_dir``. Always writes ``geometry_grouped.json`` (static, yaris-
    compatible) and ``parts_grouped.json``. When ``include_animation``,
    also writes ``frames.bin`` (raw float32 ``[T, M, 3]``) and
    ``frames.json`` (manifest + per-edge index lists into the M active
    vertices) so an animated report can play the deformation through
    the run's recorded time-steps.

    Returns a summary dict with timing, group count, output sizes, and
    any size-budget warnings. Pure function — does not touch any
    framework state beyond the supplied paths and log callback.
    """
    _log = log or (lambda _msg: None)
    size_gb = os.path.getsize(a4db_path) / 1e9
    _log(f"file: {os.path.basename(a4db_path)} ({size_gb:.2f} GB)")
    t_total = time.time()

    with h5py.File(a4db_path, "r") as h5:
        nodes_g = h5["/model_0/geometry_0/nodes_0/grids"]
        node_ids = nodes_g["ids"][:]
        coords = np.stack(
            [nodes_g["coordinates/x"][:],
             nodes_g["coordinates/y"][:],
             nodes_g["coordinates/z"][:]],
            axis=1,
        ).astype(np.float32)
        n_nodes = len(coords)
        _log(f"nodes: {n_nodes:,}")

        nid_to_idx = _build_id_lut(node_ids)

        # Normalise + Y-up swap once. Keep original `coords` around for
        # animation extraction (per-frame: base + displacement, then
        # apply the same transform).
        mn, mx = coords.min(0), coords.max(0)
        center = (mn + mx) / 2
        scale = float((mx - mn).max() / 2) or 1.0
        normed = (coords - center) / scale
        coords_yup = np.stack(
            [normed[:, 0], normed[:, 2], -normed[:, 1]], axis=1
        ).astype(np.float32)
        del normed

        elems = h5["/model_0/geometry_0/elements_0"]
        quads_topo, quads_ids = _read_topology(elems, "quads", 4, n_nodes, nid_to_idx)
        hexas_topo, hexas_ids = _read_topology(elems, "hexas", 8, n_nodes, nid_to_idx)
        beams_topo, beams_ids = _read_topology(elems, "beams", 2, n_nodes, nid_to_idx)
        _log(
            f"elements: quads={len(quads_ids):,}  "
            f"hexas={len(hexas_ids):,}  beams={len(beams_ids):,}"
        )

        props_g = h5["/model_0/geometry_0/properties_0"]
        prop_groups, types_used = _read_properties(
            props_g, len(quads_ids), len(hexas_ids), len(beams_ids), _log,
        )
        _log(f"property groups: {len(prop_groups)}  used: {sorted(types_used)}")

        t = time.time()
        bq_edges, bq_parents, iq_edges = _shell_edges(quads_topo)
        _log(
            f"shell edges: boundary={len(bq_edges):,} interior={len(iq_edges):,}  "
            f"({time.time()-t:.1f}s)"
        )

        t = time.time()
        bh_edges, bh_parents = _hex_skin_edges(hexas_topo)
        _log(f"hex skin edges: {len(bh_edges):,}  ({time.time()-t:.1f}s)")

        t = time.time()
        groups_out = _group_edges(
            prop_groups,
            bq_edges, bq_parents,
            bh_edges, bh_parents,
            beams_topo,
            n_quads=len(quads_ids),
            n_hexas=len(hexas_ids),
            n_beams=len(beams_ids),
            max_hex_per=max_hex_edges_per_group,
            max_quad_per=max_quad_edges_per_group,
        )
        _log(f"grouped edges into {len(groups_out)} titles  ({time.time()-t:.1f}s)")

        if len(iq_edges) > max_interior_edges:
            rng = np.random.default_rng(42)
            sub = rng.choice(len(iq_edges), max_interior_edges, replace=False)
            iq_edges = iq_edges[sub]
        interior_flat = _edges_to_flat(iq_edges, coords_yup)

        out_geo = {
            "interior": interior_flat,
            "groups": {
                title: _edges_to_flat(edges, coords_yup)
                for title, edges in groups_out.items()
            },
        }
        out_geo_path = os.path.join(out_dir, "geometry_grouped.json")
        with open(out_geo_path, "w") as f:
            json.dump(out_geo, f)
        geo_mb = os.path.getsize(out_geo_path) / 1e6
        _log(f"wrote geometry_grouped.json: {geo_mb:.1f} MB")

        # ---- animation: per-frame positions of active nodes ----
        anim_summary: dict[str, Any] = {}
        if include_animation:
            t = time.time()
            anim_summary = _extract_and_write_animation(
                h5,
                coords_orig=coords,
                center=center,
                scale=scale,
                groups_edges=groups_out,
                interior_edges=iq_edges,
                frame_stride=frame_stride,
                out_dir=out_dir,
                log=_log,
            )
            _log(
                f"animation: {anim_summary['frame_count']} frames, "
                f"{anim_summary['active_nodes']:,} active nodes, "
                f"frames.bin={anim_summary['frames_bin_mb']:.1f} MB "
                f"({time.time()-t:.1f}s)"
            )

        parts_out = {
            title: [{
                "id": info["pid"],
                "type": info["type"],
                "elements": info["n_elements"],
            }]
            for title, info in prop_groups.items()
            if title in out_geo["groups"]
        }
        out_parts_path = os.path.join(out_dir, "parts_grouped.json")
        with open(out_parts_path, "w") as f:
            json.dump(parts_out, f)
        _log(f"wrote parts_grouped.json: {len(parts_out)} groups")

    elapsed = time.time() - t_total
    total_group_edges = sum(len(v) // 6 for v in out_geo["groups"].values())
    warnings: list[str] = []
    if geo_mb > IFRAME_JSON_LIMIT_MB:
        warnings.append(
            f"geometry_grouped.json is {geo_mb:.1f} MB — "
            f"> {IFRAME_JSON_LIMIT_MB:.0f} MB risks iframe parse failure"
        )

    summary = {
        "ok": True,
        "elapsed_s": round(elapsed, 1),
        "groups": len(out_geo["groups"]),
        "geometry_mb": round(geo_mb, 2),
        "interior_edges": len(iq_edges),
        "total_group_edges": total_group_edges,
        "warnings": warnings,
    }
    if anim_summary:
        summary["animation"] = anim_summary
    return summary


# ---- internals -----------------------------------------------------------


def _build_id_lut(ids):
    """Return a callable id_array -> idx_array.

    Uses a dense LUT when ID range is reasonable, else searchsorted.
    """
    ids = np.asarray(ids, dtype=np.int64)
    max_id = int(ids.max())
    if max_id < 8 * len(ids):
        lut = np.full(max_id + 1, -1, dtype=np.int32)
        lut[ids] = np.arange(len(ids), dtype=np.int32)

        def f(q):
            q = np.asarray(q, dtype=np.int64)
            out = lut[q]
            if (out < 0).any():
                missing = int((out < 0).sum())
                raise ValueError(f"{missing} ids not found in LUT")
            return out
        return f

    sort_idx = np.argsort(ids)
    sorted_ids = ids[sort_idx]

    def f(q):
        q = np.asarray(q, dtype=np.int64)
        pos = np.searchsorted(sorted_ids, q)
        bad = (
            (pos >= len(sorted_ids))
            | (sorted_ids[np.minimum(pos, len(sorted_ids) - 1)] != q)
        )
        if bad.any():
            raise ValueError(f"{int(bad.sum())} ids not found via searchsorted")
        return sort_idx[pos].astype(np.int32)
    return f


def _read_topology(elems_g, kind, k, n_nodes, nid_to_idx):
    """Return (topology as (E, k) int32, ids array). Detects whether topology
    stores 0-based indices or node IDs and converts to 0-based indices."""
    if kind not in elems_g:
        return np.zeros((0, k), dtype=np.int32), np.zeros(0, dtype=np.int64)
    g = elems_g[kind]
    ids = g["ids"][:]
    raw = g["topologies"][:]
    if len(raw) == 0:
        return np.zeros((0, k), dtype=np.int32), ids
    topo = raw.reshape(-1, k).astype(np.int32, copy=False)
    if int(topo.max()) >= n_nodes:
        topo = nid_to_idx(topo.astype(np.int64)).reshape(-1, k).astype(np.int32)
    return topo, ids


def _read_properties(props_g, n_quads, n_hexas, n_beams, log: LogFn):
    """Walk /properties_0/{quads,hexas,beams}/<name>.

    Each property dataset stores **0-based global topology indices** —
    a non-overlapping partition of the element array. Returns a
    ``{title: {pid, type, *_idxs, n_elements}}`` dict and the set of
    element types actually used.
    """
    type_lut = {
        "quads": ("quad", n_quads, "quad_idxs"),
        "hexas": ("hex", n_hexas, "hex_idxs"),
        "beams": ("beam", n_beams, "beam_idxs"),
    }
    prop_groups: dict[str, dict[str, Any]] = {}
    types_seen: set[str] = set()
    skipped = 0
    out_of_range = 0

    for kind, (tname, n_elements, idx_key) in type_lut.items():
        if kind not in props_g or n_elements == 0:
            continue
        kind_g = props_g[kind]
        for pname in kind_g.keys():
            pg = kind_g[pname]
            title_attr = pg.attrs.get("title")
            if isinstance(title_attr, bytes):
                title = title_attr.decode("utf-8", "replace").strip("\x00").strip()
            else:
                title = str(title_attr or "").strip()
            if not title:
                title = pname  # fallback "quad_42"
            pid_attr = pg.attrs.get("id", -1)
            try:
                pid = int(pid_attr)
            except Exception:
                pid = -1

            ds = pg.get(pname)
            if ds is None:
                ds_keys = [k for k in pg.keys() if isinstance(pg[k], h5py.Dataset)]
                if not ds_keys:
                    skipped += 1
                    continue
                ds = pg[ds_keys[0]]
            indices = ds[:].astype(np.int32, copy=False)
            if len(indices) == 0:
                continue

            if int(indices.max()) >= n_elements or int(indices.min()) < 0:
                bad = int(((indices < 0) | (indices >= n_elements)).sum())
                indices = indices[(indices >= 0) & (indices < n_elements)]
                out_of_range += bad
                if len(indices) == 0:
                    skipped += 1
                    continue

            entry = prop_groups.get(title)
            if entry is None:
                entry = {
                    "pid": pid,
                    "type": tname,
                    "quad_idxs": np.zeros(0, dtype=np.int32),
                    "hex_idxs":  np.zeros(0, dtype=np.int32),
                    "beam_idxs": np.zeros(0, dtype=np.int32),
                    "n_elements": 0,
                }
                prop_groups[title] = entry
            elif entry["type"] != tname:
                entry["type"] = "mixed"

            entry[idx_key] = (
                np.concatenate([entry[idx_key], indices])
                if len(entry[idx_key]) else indices
            )
            entry["n_elements"] += len(indices)
            types_seen.add(tname)

    if skipped:
        log(f"  skipped {skipped} empty properties")
    if out_of_range:
        log(f"  dropped {out_of_range} out-of-range indices")
    return prop_groups, types_seen


def _shell_edges(quads_topo):
    """Boundary + interior edges for a quad-shell mesh.

    Returns (boundary_edges (B,2) int32, boundary_parents (B,) int32,
    interior_edges (I,2) int32). Each interior pair represents a unique
    edge shared by 2+ quads (deduped).
    """
    if len(quads_topo) == 0:
        return (np.zeros((0, 2), dtype=np.int32),
                np.zeros(0, dtype=np.int32),
                np.zeros((0, 2), dtype=np.int32))
    raw = np.stack([
        quads_topo[:, [0, 1]],
        quads_topo[:, [1, 2]],
        quads_topo[:, [2, 3]],
        quads_topo[:, [3, 0]],
    ], axis=0).reshape(-1, 2)
    parent = np.repeat(np.arange(len(quads_topo), dtype=np.int32), 4)

    # Drop degenerate edges (e.g. tri-as-quad with collapsed 4th node).
    mask = raw[:, 0] != raw[:, 1]
    raw = raw[mask]
    parent = parent[mask]

    sorted_e = np.sort(raw, axis=1)
    order = np.lexsort(sorted_e.T[::-1])
    se = sorted_e[order]
    sp = parent[order]

    diff = (se[1:] != se[:-1]).any(axis=1)
    starts = np.concatenate([[0], np.where(diff)[0] + 1])
    ends = np.concatenate([starts[1:], [len(se)]])
    counts = ends - starts

    b_idx = starts[counts == 1]
    i_idx = starts[counts > 1]
    return (
        se[b_idx].astype(np.int32),
        sp[b_idx],
        se[i_idx].astype(np.int32),
    )


def _hex_skin_edges(hexas_topo):
    """Skin a hex mesh: surface = faces shared by exactly one hex.

    Returns (unique_edges (E,2) int32, parent_hex (E,) int32).
    """
    if len(hexas_topo) == 0:
        return np.zeros((0, 2), dtype=np.int32), np.zeros(0, dtype=np.int32)

    # Standard LS-DYNA hex face winding (node-local indices 0..7).
    face_idx = np.array([
        [0, 1, 2, 3],
        [4, 5, 6, 7],
        [0, 1, 5, 4],
        [1, 2, 6, 5],
        [2, 3, 7, 6],
        [3, 0, 4, 7],
    ], dtype=np.int32)

    all_faces = hexas_topo[:, face_idx].reshape(-1, 4)
    parent_hex = np.repeat(np.arange(len(hexas_topo), dtype=np.int32), 6)

    faces_canon = np.sort(all_faces, axis=1)
    order = np.lexsort(faces_canon.T[::-1])
    fc = faces_canon[order]
    fp = parent_hex[order]
    fo = all_faces[order]

    diff = (fc[1:] != fc[:-1]).any(axis=1)
    starts = np.concatenate([[0], np.where(diff)[0] + 1])
    ends = np.concatenate([starts[1:], [len(fc)]])
    counts = ends - starts

    b_starts = starts[counts == 1]
    boundary_faces_ordered = fo[b_starts]
    boundary_parents = fp[b_starts]

    e_raw = np.concatenate([
        boundary_faces_ordered[:, [0, 1]],
        boundary_faces_ordered[:, [1, 2]],
        boundary_faces_ordered[:, [2, 3]],
        boundary_faces_ordered[:, [3, 0]],
    ], axis=0)
    e_parent = np.tile(boundary_parents, 4)

    e_sorted = np.sort(e_raw, axis=1)
    e_order = np.lexsort(e_sorted.T[::-1])
    es = e_sorted[e_order]
    ep = e_parent[e_order]
    d2 = (es[1:] != es[:-1]).any(axis=1)
    e_starts = np.concatenate([[0], np.where(d2)[0] + 1])
    return es[e_starts].astype(np.int32), ep[e_starts]


def _group_edges(
    prop_groups,
    bq_edges, bq_parents,
    bh_edges, bh_parents,
    beams_topo,
    n_quads, n_hexas, n_beams,
    max_hex_per, max_quad_per,
):
    """Per property title, gather + subsample its boundary + skin + beam
    edges into one ``(M, 2)`` int32 array."""
    titles = sorted(prop_groups.keys())
    tid_of = {t: i for i, t in enumerate(titles)}

    quad_pt = np.full(n_quads, -1, dtype=np.int32)
    hex_pt = np.full(n_hexas, -1, dtype=np.int32)
    beam_pt = np.full(n_beams, -1, dtype=np.int32)

    for title, info in prop_groups.items():
        tid = tid_of[title]
        if len(info["quad_idxs"]):
            quad_pt[info["quad_idxs"]] = tid
        if len(info["hex_idxs"]):
            hex_pt[info["hex_idxs"]] = tid
        if len(info["beam_idxs"]):
            beam_pt[info["beam_idxs"]] = tid

    q_titles = quad_pt[bq_parents] if len(bq_parents) else np.zeros(0, dtype=np.int32)
    h_titles = hex_pt[bh_parents] if len(bh_parents) else np.zeros(0, dtype=np.int32)
    b_titles = beam_pt if len(beams_topo) else np.zeros(0, dtype=np.int32)

    rng = np.random.default_rng(42)
    out: dict[str, np.ndarray] = {}
    for title in titles:
        tid = tid_of[title]
        chunks: list[np.ndarray] = []

        if len(bq_edges):
            mq = q_titles == tid
            n = int(mq.sum())
            if n:
                qe = bq_edges[mq]
                if n > max_quad_per:
                    sub = rng.choice(n, max_quad_per, replace=False)
                    qe = qe[sub]
                chunks.append(qe)

        if len(bh_edges):
            mh = h_titles == tid
            n = int(mh.sum())
            if n:
                he = bh_edges[mh]
                if n > max_hex_per:
                    sub = rng.choice(n, max_hex_per, replace=False)
                    he = he[sub]
                chunks.append(he)

        if len(beams_topo):
            mb = b_titles == tid
            n = int(mb.sum())
            if n:
                chunks.append(beams_topo[mb])

        if chunks:
            out[title] = np.concatenate(chunks, axis=0)

    return out


def _extract_and_write_animation(
    h5,
    *,
    coords_orig,
    center,
    scale,
    groups_edges,
    interior_edges,
    frame_stride,
    out_dir,
    log: LogFn,
) -> dict[str, Any]:
    """Pull per-frame deformed positions for the *active* node subset and
    write ``frames.bin`` (raw float32 ``[T, M, 3]``) plus ``frames.json``
    (manifest + per-edge dense indices) into ``out_dir``.

    Active nodes = union of node indices appearing in any rendered edge
    (groups + interior). Storing positions for that subset only keeps
    the frames file ~1-2 orders of magnitude smaller than full-mesh
    deformation arrays would be.
    """
    rg = h5["/model_0/results_0"]
    all_steps = sorted(
        (k for k in rg if k.startswith("step_")),
        key=lambda s: int(s.split("_")[1]),
    )
    if not all_steps:
        log("  no result steps found, skipping animation")
        return {}

    selected_steps = all_steps[::max(1, frame_stride)]

    # ---- active-node set + dense remap ----
    parts = []
    for arr in groups_edges.values():
        if len(arr):
            parts.append(arr.ravel())
    if len(interior_edges):
        parts.append(interior_edges.ravel())
    if not parts:
        log("  no edges, skipping animation")
        return {}
    active_sorted = np.unique(np.concatenate(parts).astype(np.int64))
    n_total = len(coords_orig)
    M = len(active_sorted)

    remap = np.full(n_total, -1, dtype=np.int32)
    remap[active_sorted] = np.arange(M, dtype=np.int32)

    # ---- per-frame deformed positions in normalised Y-up space ----
    base_active = coords_orig[active_sorted].astype(np.float32, copy=False)
    times = np.zeros(len(selected_steps), dtype=np.float32)
    positions = np.empty((len(selected_steps), M, 3), dtype=np.float32)

    for i, s in enumerate(selected_steps):
        sg = rg[s]
        try:
            times[i] = float(sg.attrs.get("time", 0.0))
        except Exception:
            times[i] = 0.0
        # Read the full displacement array, then index — h5py fancy
        # indexing on uint64 arrays into a 1-D dataset is supported but
        # slow; bulk read + numpy index is faster for ~2 M-row datasets.
        dx = sg["displacements/x"][:][active_sorted]
        dy = sg["displacements/y"][:][active_sorted]
        dz = sg["displacements/z"][:][active_sorted]
        ax = base_active[:, 0] + dx
        ay = base_active[:, 1] + dy
        az = base_active[:, 2] + dz
        # Normalise + Y-up swap (FEM X→Three X, FEM Z→Three Y, FEM Y→-Three Z).
        positions[i, :, 0] = (ax - center[0]) / scale
        positions[i, :, 1] = (az - center[2]) / scale
        positions[i, :, 2] = -(ay - center[1]) / scale

    # ---- write frames.bin (raw float32 little-endian) ----
    frames_bin_path = os.path.join(out_dir, "frames.bin")
    positions.astype("<f4", copy=False).tofile(frames_bin_path)
    bin_mb = os.path.getsize(frames_bin_path) / 1e6

    # ---- per-edge dense-index lists (each pair = one line segment) ----
    group_indices = {
        title: remap[edges].astype(np.int32).ravel().tolist()
        for title, edges in groups_edges.items()
    }
    interior_indices = (
        remap[interior_edges].astype(np.int32).ravel().tolist()
        if len(interior_edges) else []
    )

    manifest = {
        "frame_count": int(len(selected_steps)),
        "active_nodes": int(M),
        "stride": int(frame_stride),
        "total_steps": int(len(all_steps)),
        "times": times.tolist(),
        "positions_filename": "frames.bin",
        "positions_dtype": "float32",
        "positions_shape": [int(len(selected_steps)), int(M), 3],
        "interior_indices": interior_indices,
        "group_indices": group_indices,
    }
    frames_json_path = os.path.join(out_dir, "frames.json")
    with open(frames_json_path, "w") as f:
        json.dump(manifest, f)
    json_mb = os.path.getsize(frames_json_path) / 1e6

    return {
        "frame_count": int(len(selected_steps)),
        "active_nodes": int(M),
        "frames_bin_mb": round(bin_mb, 2),
        "frames_json_mb": round(json_mb, 2),
        "duration_s": float(times[-1] - times[0]) if len(times) else 0.0,
    }


def _edges_to_flat(edges, coords_yup):
    """``(M, 2)`` of node indices → flat list ``[x1,y1,z1, x2,y2,z2, ...]``."""
    if len(edges) == 0:
        return []
    a = coords_yup[edges[:, 0]]
    b = coords_yup[edges[:, 1]]
    out = np.empty((len(edges) * 2, 3), dtype=np.float32)
    out[0::2] = a
    out[1::2] = b
    return out.ravel().tolist()
