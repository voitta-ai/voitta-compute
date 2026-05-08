"""Generate ``icons/voitta.icns`` for the briefcase build.

Without this, briefcase falls back to a stock cartoon icon that the
.app, the Dock, every NSAlert, and Finder all inherit. The icon here
is a serif "V" wordmark on the deep-slate Voitta header colour
(``#0f172a`` — same as the chat header in ``frontend/src/theme.css``).

Run from the repo root:

    backend/.venv/bin/python tools/generate_icon.py

Outputs:
    icons/voitta.iconset/   (intermediate PNGs at every size macOS asks for)
    icons/voitta.icns       (final bundle icon, referenced from pyproject.toml)

Re-run after changing the brand colour or wordmark; ``build_app.sh``
auto-runs this when ``icons/voitta.icns`` is missing, so a fresh
``--clean`` build always has the right icon.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Voitta theme tokens, from frontend/src/theme.css. Mirror here so the
# icon stays visually consistent with the header strip.
HEADER_BG = (15, 23, 42, 255)     # --voitta-header-bg  #0f172a
HEADER_FG = (241, 245, 249, 255)  # --voitta-header-fg  #f1f5f9


# macOS .icns expects every one of these.  Names match Apple's
# ``iconutil`` convention; the @2x files are double-resolution
# Retina assets.
ICONSET_FILES: list[tuple[int, str]] = [
    (16,    "icon_16x16.png"),
    (32,    "icon_16x16@2x.png"),
    (32,    "icon_32x32.png"),
    (64,    "icon_32x32@2x.png"),
    (128,   "icon_128x128.png"),
    (256,   "icon_128x128@2x.png"),
    (256,   "icon_256x256.png"),
    (512,   "icon_256x256@2x.png"),
    (512,   "icon_512x512.png"),
    (1024,  "icon_512x512@2x.png"),
]


def _serif_font(size: int) -> ImageFont.FreeTypeFont:
    """Pick a system serif. Cochin first (Voitta brand pick), Georgia
    second (also macOS default), falls back to PIL default if neither
    exists — should never happen on macOS but a safe fallback keeps
    the script working under CI on plain Linux too.
    """
    candidates = [
        "/System/Library/Fonts/Supplemental/Cochin.ttc",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Times.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _render(px: int) -> Image.Image:
    img = Image.new("RGBA", (px, px), HEADER_BG)
    draw = ImageDraw.Draw(img)

    # Tune the V's size to occupy ~70% of the icon's height — leaves
    # the canonical macOS "rounded square" margin around the glyph.
    # Tiny sizes (16/32) need a slightly larger font ratio to stay
    # legible after macOS's mip-mapping kicks in.
    font_size = int(px * (0.78 if px <= 32 else 0.72))
    font = _serif_font(font_size)
    text = "V"

    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    # Centre, then nudge up ~6% — serif Vs are visually heavier at the
    # bottom because of the wide stems, so optical centre is above
    # geometric centre.
    x = (px - w) / 2 - bbox[0]
    y = (px - h) / 2 - bbox[1] - px * 0.06

    draw.text((x, y), text, fill=HEADER_FG, font=font)
    return img


def main() -> int:
    here = Path(__file__).resolve().parent.parent  # repo root
    icons_dir = here / "icons"
    iconset_dir = icons_dir / "voitta.iconset"
    icns_path = icons_dir / "voitta.icns"

    icons_dir.mkdir(exist_ok=True)
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir()

    for px, name in ICONSET_FILES:
        _render(px).save(iconset_dir / name)

    # iconutil is part of macOS dev tools; ships in /usr/bin. On non-
    # macOS hosts the script can still produce the iconset but won't
    # combine it — that's fine for cross-platform CI.
    if sys.platform != "darwin":
        print(f"non-darwin: stopped after iconset, no .icns produced", file=sys.stderr)
        return 0

    if icns_path.exists():
        icns_path.unlink()
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
        check=True,
    )
    print(f"wrote {icns_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
