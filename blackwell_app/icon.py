"""App icon drawn in code (thermal trace on a dark tile), so there is no binary asset to keep in sync."""
from pathlib import Path

from PIL import Image, ImageDraw

TRACE = [(0.10, 0.66), (0.30, 0.66), (0.40, 0.26), (0.56, 0.80), (0.65, 0.46), (0.90, 0.46)]
COLD, HOT = (45, 212, 191), (255, 122, 61)


def render(size: int = 256, dot: tuple | None = None) -> Image.Image:
    s = size * 4  # supersample, then downscale for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=(17, 21, 29, 255), outline=(52, 61, 77, 255), width=max(2, s // 64))
    pts = [(x * s, y * s) for x, y in TRACE]
    w = max(6, s // 14)
    for i in range(len(pts) - 1):
        t = i / (len(pts) - 2)
        col = tuple(int(COLD[k] + (HOT[k] - COLD[k]) * t) for k in range(3))
        d.line([pts[i], pts[i + 1]], fill=col, width=w, joint="curve")
        d.ellipse((pts[i][0] - w / 2, pts[i][1] - w / 2, pts[i][0] + w / 2, pts[i][1] + w / 2), fill=col)
    if dot:
        r = s * 0.13
        cx, cy = s * 0.80, s * 0.20
        d.ellipse((cx - r - s * 0.03, cy - r - s * 0.03, cx + r + s * 0.03, cy + r + s * 0.03), fill=(17, 21, 29, 255))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dot)
    return img.resize((size, size), Image.LANCZOS)


def ensure_ico(path: Path) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        render(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path
