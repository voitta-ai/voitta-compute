"""Compute-script adapter for the .a4db parser. Logic lives in
``backend/app/services/a4db.py`` so it survives in source control;
this file just bridges the run_compute (ctx, args) interface to the
pure parser API.
"""

from app.services.a4db import (
    DEFAULT_MAX_HEX_EDGES_PER_GROUP,
    DEFAULT_MAX_INTERIOR_EDGES,
    DEFAULT_MAX_QUAD_EDGES_PER_GROUP,
    find_a4db_in_dir,
    parse_a4db_to_viewer_assets,
)


def run(ctx, args=None):
    args = args or {}
    handle = args.get("snapshot") or args.get("handle")
    if not handle:
        raise ValueError(
            "a4db_parse requires args={'snapshot': 'py_xxx'} naming the "
            "snapshot directory containing the .a4db file."
        )
    snap = ctx.snapshot(handle)
    snap_dir = snap["path"]
    a4db_path = find_a4db_in_dir(snap_dir)

    return parse_a4db_to_viewer_assets(
        a4db_path,
        snap_dir,
        max_interior_edges=int(
            args.get("max_interior_edges", DEFAULT_MAX_INTERIOR_EDGES)
        ),
        max_hex_edges_per_group=int(
            args.get("max_hex_edges_per_group", DEFAULT_MAX_HEX_EDGES_PER_GROUP)
        ),
        max_quad_edges_per_group=int(
            args.get("max_quad_edges_per_group", DEFAULT_MAX_QUAD_EDGES_PER_GROUP)
        ),
        include_animation=bool(args.get("include_animation", True)),
        frame_stride=int(args.get("frame_stride", 1)),
        log=ctx.log,
    )
