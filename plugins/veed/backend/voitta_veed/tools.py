"""VEED.IO editor tool registrations.

All tools read state from the open VEED project's Redux store via React
fiber introspection. No veed.io private API credentials are needed —
the eval runs in the browser tab that already has the user's session.

Access pattern:
  1. Walk the React fiber tree from ``#root`` to find the Redux store
     that owns the ``edit`` and ``timeline`` slices.
  2. Call ``store.getState()`` and extract the relevant slice.
  3. Return a clean, serialisable summary.

The fiber walk is the only veed.io-internal coupling. Should veed.io
ever move away from React, the tools will need updating — but that
scenario is effectively impossible for their scale.

Tools shipped:
  veed_project     — project identity + dimensions + export settings
  veed_composition — all timeline elements (videos, audio, texts,
                     stickers, transitions) + media source URLs
  veed_subtitles   — subtitle/caption tracks with timing
  veed_selection   — currently selected element + playhead position
  veed_frame       — thumbnail-strip frame for a video clip at a given time
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


def _unwrap(result: Any) -> dict:
    """Unwrap the eval_js envelope ``{ok, result: <payload>, logs, ms}``."""
    if not isinstance(result, dict):
        return {"ok": False, "error": "unexpected_return", "message": repr(result)[:200]}
    payload = result.get("result")
    if isinstance(payload, dict):
        return payload
    return result


# ---------------------------------------------------------------------------
# Shared JS helper — finds the Redux store that owns the editor state.
# Injected at the top of every tool's eval body.
# ---------------------------------------------------------------------------

_FIND_STORE_JS = """
function __findEditorStore() {
  const root = document.getElementById('root');
  if (!root) return null;
  const rootKey = Object.keys(root).find(k => k.startsWith('__reactContainer'));
  if (!rootKey) return null;
  let found = null;
  function scan(fiber, depth) {
    if (!fiber || depth > 80 || found) return;
    const mp = fiber.memoizedProps;
    if (mp) {
      for (const k of Object.keys(mp)) {
        try {
          const v = mp[k];
          if (v && typeof v === 'object'
              && typeof v.getState === 'function'
              && typeof v.dispatch === 'function') {
            const s = v.getState();
            if (s && 'edit' in s && 'timeline' in s) { found = v; return; }
          }
        } catch(_) {}
      }
    }
    scan(fiber.child, depth + 1);
    scan(fiber.sibling, depth + 1);
  }
  scan(root[rootKey], 0);
  return found;
}
const __store = __findEditorStore();
if (!__store) return { ok: false, error: "store_not_found",
  message: "Redux editor store not found — is this a veed.io editor tab?" };
const __edit = __store.getState().edit;
"""

_SAFE_ARR_JS = """
function __safeArr(o) {
  if (!o) return [];
  const arr = Array.isArray(o) ? o : Object.values(o);
  return arr.filter(Boolean);
}
"""


# ---------------------------------------------------------------------------
# veed_project
# ---------------------------------------------------------------------------

_PROJECT_JS = (
    _FIND_STORE_JS
    + """
const aspect = __edit.aspect || {};
const es = __edit.exportSettings || {};
const projectId = location.pathname.match(/edit\\/([^/]+)/)?.[1] ?? es.projectId ?? null;

// duration: prefer explicit fields, fall back to max trim_end across all videos
function __safeArr(o) { if (!o) return []; return (Array.isArray(o) ? o : Object.values(o)).filter(Boolean); }
const explicitDuration = __edit.outputDuration ?? __edit.duration ?? __edit.totalDuration ?? null;
const derivedDuration = explicitDuration ?? (__safeArr(__edit.videos).reduce((mx, v) => Math.max(mx, v.trimEnd ?? 0), 0) || null);

return {
  ok: true,
  project_id:       projectId,
  name:             es.projectName ?? null,
  url:              location.href,
  duration_s:       derivedDuration,
  fps:              __edit.fps ?? null,
  width:            aspect.width  ?? null,
  height:           aspect.height ?? null,
  aspect_id:        aspect.id     ?? null,
  background_color: __edit.backgroundColor ?? null,
  version:          __edit.version ?? null,
  export_settings: {
    resolution_scale: es.resolutionScale ?? null,
    fps_limit:        es.fpsLimit        ?? null,
    burn_subtitles:   es.burnSubtitles   ?? null,
  },
};
"""
)


async def _veed_project(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        result = await call_browser("eval_js", {"js": _PROJECT_JS}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return _unwrap(result)


registry.register(
    ToolSpec(
        name="veed_project",
        description=(
            "Return identity and dimension metadata for the VEED project "
            "currently open in the editor tab.\n\n"
            "Fields returned:\n"
            "  project_id       — UUID from the editor URL.\n"
            "  name             — project display name.\n"
            "  url              — full browser URL.\n"
            "  duration_s       — total project duration in seconds.\n"
            "  fps              — frame rate.\n"
            "  width / height   — canvas dimensions (logical pixels).\n"
            "  background_color — hex colour of the canvas background.\n"
            "  version          — internal store version counter.\n"
            "  export_settings  — resolution_scale, fps_limit, burn_subtitles.\n\n"
            "Only works on www.veed.io editor pages (/edit/...)."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_veed_project,
        side="hybrid",
    )
)


# ---------------------------------------------------------------------------
# veed_composition
# ---------------------------------------------------------------------------

_COMPOSITION_JS = (
    _FIND_STORE_JS
    + _SAFE_ARR_JS
    + """
// Video clips
const videos = __safeArr(__edit.videos).map(v => ({
  uuid:         v.uuid,
  name:         v.name,
  media_source: v.mediaSource,
  start_time:   v.startTime,
  trim_start:   v.trimStart,
  trim_end:     v.trimEnd,
  duration:     (v.trimEnd ?? 0) - (v.trimStart ?? 0),
  translation:  v.translation,
  size:         v.size,
  rotation:     v.rotationAngle ?? 0,
  z_index:      v.zIndex,
  opacity:      v.opacity,
  volume:       v.volume,
  playback_rate: v.playbackRate,
  is_muted:     v.isMuted,
  crop:         v.crop,
  flip_x:       v.flipX,
  flip_y:       v.flipY,
  effects:      v.effects?.length ? v.effects : [],
  animation:    v.animation,
  background_removed: v.backgroundRemovedEnabled,
  clean_audio:        v.cleanAudioEnabled,
}));

// Audio streams
const audioStreams = __safeArr(__edit.audioStreams).map(a => ({
  uuid:       a.uuid,
  name:       a.name,
  url:        a.url,
  start_time: a.startTime,
  trim_start: a.trimStart,
  trim_end:   a.trimEnd,
  duration:   (a.trimEnd ?? 0) - (a.trimStart ?? 0),
  volume:     a.volume,
  is_muted:   a.isMuted,
}));

// Text elements (stored under 'text' as array)
const texts = __safeArr(__edit.text).map(t => ({
  uuid:        t.uuid,
  text:        t.text,
  font_family: t.fontFamily,
  font_size:   t.fontSize,
  color:       t.color,
  bold:        t.bold,
  italic:      t.italic,
  start_time:  t.startTime,
  duration:    t.duration,
  translation: t.translation,
  size:        t.size,
}));

// Stickers
const stickers = __safeArr(__edit.stickers).map(s => ({
  uuid:       s.uuid,
  name:       s.name,
  type:       s.type,
  url:        s.url,
  start_time: s.startTime,
  duration:   s.duration,
  translation: s.translation,
  size:       s.size,
}));

// Transitions
const transitions = __safeArr(__edit.transitions).map(t => ({
  uuid:       t.uuid,
  type:       t.type,
  duration:   t.duration,
  start_time: t.startTime,
}));

// Groups
const groups = __safeArr(__edit.groups).map(g => ({
  uuid:       g.uuid,
  name:       g.name,
  element_ids: g.elementIds || g.elements || [],
}));

// Media sources (the actual source files behind mediaSource UUIDs)
const mediaSources = Object.entries(__edit.mediaSources || {}).map(([id, ms]) => ({
  id,
  asset_id:    ms.assetId,
  online_url:  ms.onlineURL,
  proxy_url:   ms.proxyURL,
  display_url: ms.displayURL,
  duration:    ms.duration,
  resolution:  ms.resolution,
  thumbnails:  ms.thumbnails ? {
    url:          ms.thumbnails.url,
    frame_count:  ms.thumbnails.frameCount,
    frame_width:  ms.thumbnails.frameWidth,
    frame_height: ms.thumbnails.frameHeight,
  } : null,
}));

return {
  ok: true,
  counts: {
    videos: videos.length,
    audio_streams: audioStreams.length,
    texts: texts.length,
    stickers: stickers.length,
    transitions: transitions.length,
    groups: groups.length,
    media_sources: mediaSources.length,
  },
  videos,
  audio_streams: audioStreams,
  texts,
  stickers,
  transitions,
  groups,
  media_sources: mediaSources,
};
"""
)


async def _veed_composition(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        result = await call_browser("eval_js", {"js": _COMPOSITION_JS}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return _unwrap(result)


registry.register(
    ToolSpec(
        name="veed_composition",
        description=(
            "Return every timeline element in the open VEED project.\n\n"
            "Reads the Redux editor store directly via React fiber "
            "introspection — no API calls, no credentials needed.\n\n"
            "Response structure:\n"
            "  counts          — element counts per category.\n"
            "  videos[]        — video clips: uuid, name, media_source,\n"
            "                    start/trim times, size, translation,\n"
            "                    opacity, volume, effects, animation, etc.\n"
            "  audio_streams[] — audio-only clips with timing + volume.\n"
            "  texts[]         — text overlays with content + style.\n"
            "  stickers[]      — image/GIF overlays.\n"
            "  transitions[]   — cut transitions between clips.\n"
            "  groups[]        — named element groups.\n"
            "  media_sources[] — backing source files keyed by asset id:\n"
            "                    online_url, proxy_url, duration,\n"
            "                    resolution, thumbnail strip url.\n\n"
            "Use veed_project first to get the project name and duration. "
            "Only works on www.veed.io editor pages (/edit/...)."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_veed_composition,
        side="hybrid",
    )
)


# ---------------------------------------------------------------------------
# veed_subtitles
# ---------------------------------------------------------------------------

_SUBTITLES_JS = """
return (async () => {
  // VEED stores subtitle cues in a Draft.js editorState prop on a React component,
  // NOT in Redux. We find it by locating a subtitle row element and walking the fiber.
  // Auto-click the Subtitles button if the panel isn't already open
  let rowEl = document.querySelector('[data-subtitle-row-uuid]');
  if (!rowEl) {
    const allBtns = document.querySelectorAll('button, a');
    const subtitlesBtn = Array.from(allBtns).find(
      b => b.textContent?.trim() === 'Subtitles' && b.getBoundingClientRect().x < 120
    );
    if (subtitlesBtn) {
      subtitlesBtn.click();
      // Wait up to 3s for Draft.js editor to mount
      for (let i = 0; i < 30; i++) {
        await new Promise(r => setTimeout(r, 100));
        if (document.querySelector('[data-subtitle-row-uuid]')) break;
      }
      rowEl = document.querySelector('[data-subtitle-row-uuid]');
    }
  }
  if (!rowEl) return {ok: true, count: 0, items: [], note: 'No subtitle cues found'};

  const fk = Object.keys(rowEl).find(k => k.startsWith('__reactFiber'));
  let cur = rowEl[fk];
  let editorState = null;
  let depth = 0;
  while (cur && depth < 50) {
    const mp = cur.memoizedProps;
    if (mp && mp.editorState && typeof mp.editorState.getCurrentContent === 'function') {
      editorState = mp.editorState; break;
    }
    cur = cur.return; depth++;
  }
  if (!editorState) return {ok: false, error: 'editorState not found'};

  const content = editorState.getCurrentContent();
  const blocks = [...content.getBlockMap().values()];
  const items = blocks.map(b => {
    const d = b.getData().toJS();
    return {
      uuid:       d.uuid,
      text:       b.getText(),
      start_time: d.from ?? null,
      end_time:   d.to   ?? null,
      track_uuid: d.trackUuid ?? null,
      words:      (d.words || []).map(w => ({word: w.value, from: w.from, to: w.to})),
    };
  });

  // Get font/language from Redux subtitles slice
  const root = document.getElementById('root');
  const rk2 = Object.keys(root).find(k => k.startsWith('__reactContainer') || k.startsWith('__reactFiber'));
  let store2 = null;
  const seen2 = new WeakSet();
  function walkF2(f, d) {
    if (!f || d > 40 || seen2.has(f)) return;
    seen2.add(f);
    const mp = f.memoizedProps;
    if (mp) for (const val of Object.values(mp)) {
      if (val && typeof val.getState === 'function' && typeof val.dispatch === 'function') {
        const st = val.getState();
        if (st && st.edit && st.timeline) { store2 = val; return; }
      }
    }
    walkF2(f.child, d+1); walkF2(f.sibling, d+1);
  }
  walkF2(root[rk2], 0);
  const subs = store2?.getState()?.edit?.subtitles || {};

  return {
    ok:              true,
    count:           items.length,
    items,
    active_language: subs.activeLanguage ?? null,
    font:            subs.font ?? null,
  };
})();
"""


async def _veed_subtitles(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        result = await call_browser("eval_js", {"js": _SUBTITLES_JS}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return _unwrap(result)


registry.register(
    ToolSpec(
        name="veed_subtitles",
        description=(
            "Return the subtitle / caption track for the open VEED project.\n\n"
            "Fields:\n"
            "  active_language — ISO language code, or 'default'.\n"
            "  font            — subtitle font family.\n"
            "  count           — number of subtitle cues.\n"
            "  items[]         — subtitle cues: uuid, text, start_time,\n"
            "                    end_time, duration, language.\n"
            "  meta_keys       — other subtitle metadata available in\n"
            "                    the store (status flags, settings).\n\n"
            "Returns count=0 and items=[] when no subtitles have been "
            "generated yet. Only works on www.veed.io editor pages."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_veed_subtitles,
        side="hybrid",
    )
)


# ---------------------------------------------------------------------------
# veed_selection
# ---------------------------------------------------------------------------

_SELECTION_JS = (
    _FIND_STORE_JS
    + _SAFE_ARR_JS
    + """
const tl = __store.getState().timeline;

// Which element is selected / focused?
const selectedVideoId   = __edit.selectedVideo    ?? null;
const focusedElementId  = __edit.focusedElement   ?? null;
const selectedElements  = __edit.selectedElements ?? [];  // multi-select UUIDs

// Resolve the focused element's full descriptor
function resolveElement(uuid) {
  if (!uuid) return null;
  const allBuckets = ['videos','audioStreams','text','stickers','progressBars'];
  for (const bucket of allBuckets) {
    const items = __safeArr(__edit[bucket]);
    const hit = items.find(x => x.uuid === uuid);
    if (hit) return { bucket, uuid: hit.uuid, name: hit.name ?? null,
                      start_time: hit.startTime, trim_start: hit.trimStart ?? null,
                      trim_end: hit.trimEnd ?? null, media_source: hit.mediaSource ?? null };
  }
  return { uuid, bucket: 'unknown' };
}

const focused = resolveElement(focusedElementId || selectedVideoId);
const multiSelected = selectedElements.map(resolveElement).filter(Boolean);

// Playhead
const currentTime  = tl.currentTime  ?? null;
const currentFrame = tl.currentFrame ?? null;

// Which subtitle cue is at the playhead?
let activeCue = null;
if (currentTime !== null) {
  const subs = __edit.subtitles || {};
  let items = [];
  if (Array.isArray(subs.items)) items = subs.items;
  else if (Array.isArray(subs.data)) items = subs.data;
  const cue = items.find(s => (s.startTime ?? s.start) <= currentTime
                           && (s.endTime ?? s.end) >= currentTime);
  if (cue) activeCue = { uuid: cue.uuid, text: cue.text,
                         start_time: cue.startTime ?? cue.start,
                         end_time: cue.endTime ?? cue.end };
}

return {
  ok: true,
  focused_element:   focused,
  multi_selected:    multiSelected,
  selection_count:   selectedElements.length,
  playhead_time_s:   currentTime,
  playhead_frame:    currentFrame,
  active_subtitle_cue: activeCue,
};
"""
)


async def _veed_selection(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        result = await call_browser("eval_js", {"js": _SELECTION_JS}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return _unwrap(result)


registry.register(
    ToolSpec(
        name="veed_selection",
        description=(
            "Return what is currently selected in the VEED editor and where the playhead is.\n\n"
            "Fields:\n"
            "  focused_element      — the element the user clicked / highlighted:\n"
            "                         { bucket, uuid, name, start_time, trim_start,\n"
            "                           trim_end, media_source }. null if nothing.\n"
            "  multi_selected[]     — all elements in a multi-select (usually empty).\n"
            "  selection_count      — number of elements in multi-select.\n"
            "  playhead_time_s      — current playhead position in seconds.\n"
            "  playhead_frame       — current frame index (null if not scrubbing).\n"
            "  active_subtitle_cue  — subtitle cue visible at the playhead, or null.\n\n"
            "Call this to understand what the user is looking at before making\n"
            "suggestions. Combine with veed_frame to show the user the frame\n"
            "at the current playhead position."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_veed_selection,
        side="hybrid",
    )
)


# ---------------------------------------------------------------------------
# veed_frame
# ---------------------------------------------------------------------------

_FRAME_JS_TMPL = """
return (async () => {{
const videoUuid    = {video_uuid};
const atTime       = {at_time};    // seconds; null = use first frame
const highRes      = {high_res};   // true = Canvas/video seek; false = thumbnail strip

// ── 1. Locate the media source ──────────────────────────────────────────────
{find_store}
{safe_arr}
const videos = __safeArr(__edit.videos);
const clip = videoUuid
  ? videos.find(v => v.uuid === videoUuid)
  : videos[0];
if (!clip) return {{ ok: false, error: 'clip_not_found', video_uuid: videoUuid }};

const ms = (__edit.mediaSources || {{}})[clip.mediaSource];
if (!ms) return {{ ok: false, error: 'media_source_not_found', clip_uuid: clip.uuid }};

const thumbs = ms.thumbnails;
const totalDuration = ms.duration ?? (clip.trimEnd - clip.trimStart);

// Resolve time: null/'first' → clip.trimStart, 'last' → clip.trimEnd
let targetTime = atTime;
if (targetTime === null || targetTime === 'first') targetTime = clip.trimStart ?? 0;
if (targetTime === 'last') targetTime = clip.trimEnd ?? totalDuration;
targetTime = Math.max(0, Math.min(targetTime, totalDuration));

// ── 2. Thumbnail strip (fast, low-res) ──────────────────────────────────────
if (!highRes && thumbs && thumbs.url) {{
  const frameW = thumbs.frameWidth;
  const frameH = thumbs.frameHeight;
  const frameCount = thumbs.frameCount;
  const interval = thumbs.timeBetweenFrames ?? (totalDuration / frameCount);
  const frameIdx = Math.min(Math.round(targetTime / interval), frameCount - 1);

  const img = new Image();
  img.crossOrigin = 'anonymous';
  await new Promise((res, rej) => {{
    img.onload = res;
    img.onerror = e => rej(new Error('strip load failed'));
    img.src = thumbs.url;
  }});

  const canvas = document.createElement('canvas');
  canvas.width = frameW; canvas.height = frameH;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, frameIdx * frameW, 0, frameW, frameH, 0, 0, frameW, frameH);
  const dataUrl = canvas.toDataURL('image/jpeg', 0.85);

  return {{
    ok: true,
    project_id:   location.pathname.match(/edit\/([^/]+)/)?.[1] ?? null,
    clip_uuid:    clip.uuid,
    clip_name:    clip.name,
    target_time:  targetTime,
    frame_index:  frameIdx,
    width:        frameW,
    height:       frameH,
    source:       'thumbnail_strip',
    image:        dataUrl,
    strip_url:    thumbs.url,
    frame_count:  frameCount,
  }};
}}

// ── 3. High-res: seek a hidden <video> element ───────────────────────────────
const video = document.createElement('video');
video.crossOrigin = 'anonymous';
video.src = ms.onlineURL ?? ms.proxyURL;
video.muted = true;
video.preload = 'metadata';

await new Promise((res, rej) => {{
  video.onloadedmetadata = res;
  video.onerror = () => rej(new Error('video load failed: ' + ms.onlineURL));
  setTimeout(() => rej(new Error('metadata timeout')), 10000);
}});

video.currentTime = targetTime;
await new Promise((res, rej) => {{
  video.onseeked = res;
  video.onerror = rej;
  setTimeout(() => rej(new Error('seek timeout')), 8000);
}});

const vw = ms.resolution?.width  ?? video.videoWidth;
const vh = ms.resolution?.height ?? video.videoHeight;
// Cap at 1280px wide to keep base64 manageable
const scale = Math.min(1, 1280 / vw);
const outW = Math.round(vw * scale);
const outH = Math.round(vh * scale);

const canvas = document.createElement('canvas');
canvas.width = outW; canvas.height = outH;
const ctx = canvas.getContext('2d');
ctx.drawImage(video, 0, 0, outW, outH);
const dataUrl = canvas.toDataURL('image/jpeg', 0.85);

return {{
  ok: true,
  project_id:  location.pathname.match(/edit\/([^/]+)/)?.[1] ?? null,
  clip_uuid:   clip.uuid,
  clip_name:   clip.name,
  target_time: targetTime,
  width:       outW,
  height:      outH,
  source:      'video_seek',
  image:       dataUrl,
}};
}})()
"""


async def _veed_frame(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    video_uuid = args.get("video_uuid")
    at_time    = args.get("at_time")    # seconds | "first" | "last" | null
    high_res   = bool(args.get("high_res", False))

    if isinstance(at_time, str) and at_time not in ("first", "last"):
        try:
            at_time = float(at_time)
        except ValueError:
            return {"ok": False, "error": "bad_request",
                    "message": "at_time must be a number, 'first', or 'last'"}

    # Serialise args into the JS template
    uuid_js = f'"{video_uuid}"' if video_uuid else "null"
    time_js = (f'"{at_time}"' if isinstance(at_time, str)
               else str(at_time) if at_time is not None else "null")
    high_res_js = "true" if high_res else "false"

    js = _FRAME_JS_TMPL.format(
        video_uuid=uuid_js,
        at_time=time_js,
        high_res=high_res_js,
        find_store=_FIND_STORE_JS.strip(),
        safe_arr=_SAFE_ARR_JS.strip(),
    )

    timeout = 35_000 if high_res else 15_000
    try:
        result = await call_browser("eval_js", {"js": js, "await_ms": timeout}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    if not isinstance(result, dict):
        return {"ok": False, "error": "unexpected_return", "message": repr(result)[:200]}

    # eval_js wraps the JS return value under "result" key:
    # { ok: true, result: { ok, image, clip_name, ... }, logs, ms }
    payload = _unwrap(result)
    if not payload.get("ok"):
        return payload

    # Write image into python_storage so it appears in the Workspace DATA panel.
    # Base64 data URLs are stripped before reaching the model — the Read tool
    # displays images natively when given the file path.
    data_url = payload.pop("image", None)
    if data_url:
        import base64
        import tempfile
        from app.services.python_storage import put_file
        clip_name = payload.get("clip_name") or "clip"
        t = payload.get("target_time", 0)
        fname = f"{clip_name}_{t:.2f}s.jpg"
        # Write to a temp file first; put_file will move it into the snapshot dir.
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            header, b64 = data_url.split(",", 1)
            tmp.write(base64.b64decode(b64))
            tmp_path = tmp.name
        # Build a human-readable label: "clip_name @ Xs (first|last|Xs)"
        at_label = (
            "first frame" if at_time in (None, "first", 0) else
            "last frame"  if at_time == "last" else
            f"@ {t:.2f}s"
        )
        label = f"{clip_name} — {at_label}"

        snap = put_file(
            src_path=tmp_path,
            original_name=fname,
            kind="veed_frame",
            meta={
                "label":          label,
                "source":         payload.get("source"),
                # veed project context
                "project_id":     payload.get("project_id"),
                "clip_uuid":      payload.get("clip_uuid"),
                "clip_name":      clip_name,
                # frame context
                "target_time_s":  t,
                "at":             at_time if isinstance(at_time, str) else f"{t:.2f}s",
                "frame_index":    payload.get("frame_index"),
                "frame_count":    payload.get("frame_count"),
                "strip_url":      payload.get("strip_url"),
                # image dimensions
                "width":          payload.get("width"),
                "height":         payload.get("height"),
                "high_res":       high_res,
            },
            move=True,
            folder_name=args.get("folder_name") or None,
        )
        payload["file"] = str(snap["path"]) + "/" + fname
        payload["handle"] = snap["handle"]
        payload["label"] = label
        payload["note"] = "Use the Read tool on 'file' to view the image."

    return payload


registry.register(
    ToolSpec(
        name="veed_frame",
        description=(
            "Return a frame from a video clip in the open VEED project as a JPEG image.\n\n"
            "Parameters:\n"
            "  video_uuid  — UUID of the clip (from veed_composition). Omit to use\n"
            "                the first (or only) video clip.\n"
            "  at_time     — When to sample. Accepts:\n"
            "                  'first'   — first frame of the clip (default)\n"
            "                  'last'    — last frame of the clip\n"
            "                  <number>  — seconds from the start of the media file\n"
            "  high_res    — false (default): fast 98×54 thumbnail-strip frame.\n"
            "                true: full-resolution seek via a hidden <video> element\n"
            "                (up to 1280px wide JPEG, takes 5–15 s).\n\n"
            "Response fields:\n"
            "  file         — path to a JPEG file; use the Read tool to view it.\n"
            "  width/height — pixel dimensions of the image.\n"
            "  target_time  — actual time sampled (seconds).\n"
            "  frame_index  — strip frame index (thumbnail_strip source only).\n"
            "  source       — 'thumbnail_strip' or 'video_seek'.\n\n"
            "IMPORTANT: always follow up with Read tool on the returned 'file'\n"
            "path to actually see the image — it is not shown automatically.\n\n"
            "Use high_res=false first for a quick glance; switch to high_res=true\n"
            "for detail inspection. Only works on www.veed.io editor pages."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "video_uuid": {
                    "type": "string",
                    "description": "UUID of the video clip. Omit to use the first clip.",
                },
                "at_time": {
                    "description": "'first', 'last', or a number (seconds).",
                },
                "high_res": {
                    "type": "boolean",
                    "description": "Full-res video seek instead of thumbnail strip.",
                    "default": False,
                },
                "folder_name": {
                    "type": "string",
                    "description": "Workspace folder to store the frame in. Create with create_folder first.",
                },
            },
            "additionalProperties": False,
        },
        handler=_veed_frame,
        side="hybrid",
    )
)
