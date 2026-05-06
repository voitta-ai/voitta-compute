"""Animated wireframe viewer for a4db_parse output.

Loads geometry_grouped.json (interior edges as static flat coords) +
frames.json (per-edge dense indices into the active-vertex set) +
frames.bin (raw float32 [T, M, 3] positions) from the most-recent
snapshot containing all three. Three.js renders via a shared position
attribute so all 324 groups + interior animate from one float copy
per frame.

If the raw frames.bin would exceed the iframe base64 budget (~10 MB
encoded), frames are uniformly subsampled at build time. The static
shell silhouette is always shown, even when animation is unavailable.
"""

from __future__ import annotations

import base64
import html
import json
import os
import time

import numpy as np
import panel as pn

# Iframe srcdoc base64 binary ceiling — see docs/09-panel-threejs-reports.md
# (10-15 MB encoded). Middle of that range, leaves headroom for JS + indices.
MAX_BASE64_BYTES = 12 * 1024 * 1024


def _find_animated_snapshot(ctx):
    """Return the most recent python_storage snapshot containing the
    full animation triple (frames.bin + frames.json + geometry_grouped.json)."""
    from app.services import python_storage as ps

    best: tuple[float, str] | None = None
    for d in ps.STORAGE_ROOT.iterdir():
        if not d.is_dir():
            continue
        needed = ("frames.bin", "frames.json", "geometry_grouped.json")
        if not all((d / n).exists() for n in needed):
            continue
        mtime = (d / "frames.bin").stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, str(d))
    if best is None:
        raise RuntimeError(
            "No snapshot with frames.bin + frames.json + geometry_grouped.json. "
            "Run the a4db_parse compute script against your .a4db snapshot first."
        )
    return best[1]


def _hash_color(name: str) -> str:
    """Deterministic group → hex colour. Skips dim/blue hues since the
    background is dark blue and interior edges are dim blue."""
    h = 0
    for ch in name:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    hue = (h % 300 + 30) / 360.0  # avoid pure blue (~240°)
    sat, lit = 0.65, 0.62
    # HSL -> RGB
    c = (1 - abs(2 * lit - 1)) * sat
    x = c * (1 - abs(((hue * 6) % 2) - 1))
    m = lit - c / 2
    seg = int(hue * 6) % 6
    rgb = [
        (c, x, 0), (x, c, 0), (0, c, x),
        (0, x, c), (x, 0, c), (c, 0, x),
    ][seg]
    r, g, b = (int((v + m) * 255) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def build(ctx):
    snap_dir = _find_animated_snapshot(ctx)

    # ---- load static interior (for context, never animated) ----
    with open(os.path.join(snap_dir, "geometry_grouped.json")) as f:
        geo = json.load(f)
    interior_static_flat = geo.get("interior", [])

    # ---- load animation manifest ----
    with open(os.path.join(snap_dir, "frames.json")) as f:
        anim = json.load(f)
    T0, M, _ = anim["positions_shape"]
    times = np.array(anim["times"], dtype=np.float32)

    # ---- load full positions, then quantize to int16 to halve payload ----
    positions = np.fromfile(
        os.path.join(snap_dir, "frames.bin"), dtype=np.float32
    ).reshape(T0, M, 3)
    # Range covers the post-crash fragment expansion observed in tests
    # (peak ≈ ±9 in unit-cube-normalised space).
    quant_scale = 32767.0 / 9.0
    pos_q_full = np.clip(np.round(positions * quant_scale), -32768, 32767).astype("<i2")
    bytes_per_frame = M * 3 * 2  # int16

    # ---- subsample frames to fit base64 budget ----
    raw_bytes_total = T0 * bytes_per_frame
    stride = 1
    while raw_bytes_total // stride > MAX_BASE64_BYTES * 3 // 4:
        stride += 1
    frame_idxs = list(range(0, T0, stride))
    if frame_idxs[-1] != T0 - 1:
        frame_idxs.append(T0 - 1)
    T = len(frame_idxs)

    pos_q = pos_q_full[frame_idxs]
    times_sub = times[frame_idxs]
    pos_bytes = pos_q.tobytes()
    pos_b64 = base64.b64encode(pos_bytes).decode("ascii")
    pos_b64_mb = len(pos_b64) / 1e6

    # ---- group indices: build a packed Uint32 buffer + per-group ranges ----
    # Encoding: one Uint32Array contains all groups' indices concatenated;
    # group_offsets gives [start, end] per group.
    group_indices = anim["group_indices"]
    interior_indices = anim.get("interior_indices") or []

    titles = sorted(group_indices.keys())
    colors = {t: _hash_color(t) for t in titles}

    # Pack indices into a single Uint32Array. Each contiguous slice = one group.
    chunks: list[np.ndarray] = []
    offsets: list[tuple[int, int]] = []
    for t in titles:
        arr = np.asarray(group_indices[t], dtype=np.uint32)
        start = sum(c.size for c in chunks)
        chunks.append(arr)
        offsets.append((start, start + arr.size))
    interior_arr = np.asarray(interior_indices, dtype=np.uint32)
    interior_offset = (sum(c.size for c in chunks),
                       sum(c.size for c in chunks) + interior_arr.size)
    chunks.append(interior_arr)

    indices_buf = np.concatenate(chunks).astype("<u4", copy=False) if chunks else np.zeros(0, dtype=np.uint32)
    indices_b64 = base64.b64encode(indices_buf.tobytes()).decode("ascii")

    # ---- summary block (chat-side) ----
    head = (
        f"**a4db_3d** — `{os.path.basename(snap_dir)}`  \n"
        f"Active vertices: **{M:,}**  \n"
        f"Frames: **{T}** (every {stride} of {T0}, t=0…{times[-1]:.3f}s)  \n"
        f"Groups: **{len(titles)}**  \n"
        f"Embedded payload: positions {pos_b64_mb:.1f} MB · "
        f"indices {len(indices_b64)/1e6:.1f} MB  \n"
        f"Drag to orbit · scroll to zoom · space to pause"
    )

    # ---- viewer HTML ----
    viewer_html = _viewer_html(
        positions_b64=pos_b64,
        indices_b64=indices_b64,
        active_count=M,
        frame_count=T,
        group_offsets=offsets,
        group_titles=titles,
        group_colors=colors,
        interior_offset=interior_offset,
        interior_static_flat=interior_static_flat,
        times=times_sub.tolist(),
        quant_scale=quant_scale,
    )

    iframe = pn.pane.HTML(
        f'<iframe srcdoc="{html.escape(viewer_html, quote=True)}" '
        f'style="width:100%;height:720px;border:0;background:#05080f"></iframe>',
        sizing_mode="stretch_width",
    )

    return pn.Column(
        pn.pane.Markdown(head, sizing_mode="stretch_width"),
        iframe,
        sizing_mode="stretch_width",
    )


def _viewer_html(
    *,
    positions_b64: str,
    indices_b64: str,
    active_count: int,
    frame_count: int,
    group_offsets: list[tuple[int, int]],
    group_titles: list[str],
    group_colors: dict[str, str],
    interior_offset: tuple[int, int],
    interior_static_flat: list[float],
    times: list[float],
    quant_scale: float,
) -> str:
    groups_meta = json.dumps([
        {"title": t, "start": s, "end": e, "color": group_colors[t]}
        for t, (s, e) in zip(group_titles, group_offsets)
    ])
    interior_meta = json.dumps({"start": interior_offset[0], "end": interior_offset[1]})
    times_js = json.dumps(times)
    interior_static_js = json.dumps(interior_static_flat)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{ width:100%; height:100%; background:#05080f; overflow:hidden;
              font-family:monospace; color:#aac8e8; }}
#wrap {{ position:relative; width:100%; height:100%; }}
canvas {{ display:block; width:100% !important; height:100% !important; }}
#hud {{ position:absolute; left:12px; top:10px; color:#4a9eff;
        font:700 12px/1 monospace; letter-spacing:0.08em; text-transform:uppercase;
        pointer-events:none; }}
#playback {{ position:absolute; left:12px; right:12px; bottom:10px;
             background:#0b1424cc; backdrop-filter: blur(4px);
             padding:8px 12px; border:1px solid #1d3050; border-radius:4px;
             display:flex; align-items:center; gap:10px; font-size:11px; }}
#playback button {{ background:#1a3050; color:#aac8e8; border:1px solid #2c4878;
                    padding:4px 10px; cursor:pointer; font:700 11px monospace;
                    border-radius:3px; }}
#playback button:hover {{ background:#2c4878; }}
#scrub {{ flex:1 1 auto; -webkit-appearance:none; appearance:none;
          height:6px; background:#1a3050; border-radius:3px; cursor:pointer; }}
#scrub::-webkit-slider-thumb {{ -webkit-appearance:none; width:14px; height:14px;
          background:#4a9eff; border-radius:50%; cursor:pointer; }}
#scrub::-moz-range-thumb {{ width:14px; height:14px;
          background:#4a9eff; border:none; border-radius:50%; cursor:pointer; }}
#tlabel {{ min-width:90px; color:#7ab; }}
#flabel {{ min-width:60px; color:#4a9eff; text-align:right; }}
</style>
<script type="importmap">
{{
  "imports": {{
    "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
    "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
  }}
}}
</script>
</head>
<body>
<div id="wrap"></div>
<div id="hud">a4db crash playback</div>
<div id="playback">
  <button id="btn-play">▶ Play</button>
  <input type="range" id="scrub" min="0" max="{frame_count - 1}" value="0" step="1">
  <span id="flabel">0 / {frame_count - 1}</span>
  <span id="tlabel">t = 0.000s</span>
  <button id="btn-speed">1×</button>
</div>
<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';

const POS_B64 = "{positions_b64}";
const IDX_B64 = "{indices_b64}";
const M = {active_count};
const T = {frame_count};
const Q_SCALE = {quant_scale};
const GROUPS = {groups_meta};
const INTERIOR = {interior_meta};
const INTERIOR_STATIC = {interior_static_js};
const TIMES = {times_js};

function b64ToBytes(b64) {{
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}}

// Positions are int16 (range mapped from ±9 unit-cube space). Decode
// once into Float32Array (T*M*3) — same memory shape as before, but
// the wire payload was halved.
const posBytes = b64ToBytes(POS_B64);
const posI16 = new Int16Array(posBytes.buffer, posBytes.byteOffset, posBytes.byteLength / 2);
const positions = new Float32Array(posI16.length);
for (let i = 0; i < posI16.length; i++) positions[i] = posI16[i] / Q_SCALE;
const idxBytes = b64ToBytes(IDX_B64);
const indices = new Uint32Array(idxBytes.buffer, idxBytes.byteOffset, idxBytes.byteLength / 4);

function init() {{
  const wrap = document.getElementById('wrap');
  if (!wrap || wrap.clientWidth === 0) {{ requestAnimationFrame(init); return; }}

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x05080f);
  const W = () => wrap.clientWidth;
  const H = () => wrap.clientHeight;

  const renderer = new THREE.WebGLRenderer({{ antialias: true }});
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(W(), H());
  wrap.appendChild(renderer.domElement);

  const camera = new THREE.PerspectiveCamera(40, W()/H(), 0.01, 50);
  camera.position.set(2.0, 0.8, -2.0);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.07;

  // Shared position attribute — every group + animated interior reads from this.
  // Initialise with frame 0.
  const sharedPosArr = new Float32Array(M * 3);
  sharedPosArr.set(positions.subarray(0, M * 3));
  const sharedPos = new THREE.BufferAttribute(sharedPosArr, 3);
  sharedPos.setUsage(THREE.DynamicDrawUsage);

  // One LineSegments per group, all sharing sharedPos and indexing via a
  // sliced Uint32 attribute taken from the packed indices buffer.
  for (const g of GROUPS) {{
    const slice = indices.subarray(g.start, g.end);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', sharedPos);
    geo.setIndex(new THREE.BufferAttribute(slice, 1));
    const mat = new THREE.LineBasicMaterial({{ color: g.color, transparent:true, opacity:0.9 }});
    scene.add(new THREE.LineSegments(geo, mat));
  }}

  // Animated interior (deformed shell internals, dim).
  if (INTERIOR.end > INTERIOR.start) {{
    const slice = indices.subarray(INTERIOR.start, INTERIOR.end);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', sharedPos);
    geo.setIndex(new THREE.BufferAttribute(slice, 1));
    scene.add(new THREE.LineSegments(geo,
      new THREE.LineBasicMaterial({{ color:0x152840, transparent:true, opacity:0.35 }})));
  }}

  // Static interior — sampled deformed mesh, undeformed reference (frame 0
  // baked into geometry_grouped.json's `interior` field). Shown faintly so
  // the user has a fixed visual anchor.
  if (INTERIOR_STATIC && INTERIOR_STATIC.length) {{
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(INTERIOR_STATIC), 3));
    scene.add(new THREE.LineSegments(geo,
      new THREE.LineBasicMaterial({{ color:0x0a1a2a, transparent:true, opacity:0.25 }})));
  }}

  scene.add(new THREE.AxesHelper(0.4));

  // ---- playback ----
  let currentFrame = 0;
  let playing = true;
  let speed = 1.0;
  let lastSwitch = performance.now();
  const FRAME_MS = 90;  // 90 ms/frame ≈ 11 fps base

  const scrub = document.getElementById('scrub');
  const btnPlay = document.getElementById('btn-play');
  const btnSpeed = document.getElementById('btn-speed');
  const flabel = document.getElementById('flabel');
  const tlabel = document.getElementById('tlabel');

  function setFrame(i) {{
    currentFrame = ((i % T) + T) % T;
    sharedPosArr.set(positions.subarray(currentFrame * M * 3, (currentFrame + 1) * M * 3));
    sharedPos.needsUpdate = true;
    scrub.value = String(currentFrame);
    flabel.textContent = currentFrame + ' / ' + (T - 1);
    tlabel.textContent = 't = ' + TIMES[currentFrame].toFixed(3) + 's';
  }}

  scrub.addEventListener('input', () => {{
    playing = false; btnPlay.textContent = '▶ Play';
    setFrame(parseInt(scrub.value, 10));
  }});
  btnPlay.addEventListener('click', () => {{
    playing = !playing;
    btnPlay.textContent = playing ? '⏸ Pause' : '▶ Play';
    lastSwitch = performance.now();
  }});
  btnSpeed.addEventListener('click', () => {{
    speed = ({{ '0.5×':1, '1×':2, '2×':4, '4×':0.5 }})[btnSpeed.textContent] === undefined
      ? 1 : ({{ '0.5×':1, '1×':2, '2×':4, '4×':0.5 }})[btnSpeed.textContent];
    btnSpeed.textContent = ({{ 0.5:'0.5×', 1:'1×', 2:'2×', 4:'4×' }})[speed];
  }});
  document.addEventListener('keydown', (e) => {{
    if (e.code === 'Space') {{ e.preventDefault(); btnPlay.click(); }}
  }});

  setFrame(0);

  (function loop() {{
    requestAnimationFrame(loop);
    const now = performance.now();
    if (playing && (now - lastSwitch) > FRAME_MS / speed) {{
      lastSwitch = now;
      setFrame(currentFrame + 1);
    }}
    controls.update();
    renderer.render(scene, camera);
  }})();

  window.addEventListener('resize', () => {{
    camera.aspect = W()/H(); camera.updateProjectionMatrix();
    renderer.setSize(W(), H(), false);
  }});
}}

init();
</script>
</body></html>"""
