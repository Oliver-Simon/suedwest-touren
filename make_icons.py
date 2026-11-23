"""Generate PWA icons (192x192, 512x512, maskable 512x512) for Südwest-Touren."""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))


def draw(size: int, maskable: bool = False) -> Image.Image:
    s = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (15, 76, 92, 255)
    if maskable:
        d.rectangle([0, 0, size, size], fill=bg)
        k, off = 0.72, size * 0.14
    else:
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(14 * s), fill=bg)
        k, off = 1.0, 0.0

    def P(pts):
        return [(off + x * s * k, off + y * s * k) for x, y in pts]

    # Sun / evening glow
    sun_r = 7 * s * k
    sun_cx, sun_cy = off + 46 * s * k, off + 18 * s * k
    d.ellipse([sun_cx - sun_r, sun_cy - sun_r, sun_cx + sun_r, sun_cy + sun_r], fill=(251, 191, 36, 255))

    # Mountains / forest ridges (Pfälzerwald / Schwarzwald / Odenwald)
    d.polygon(P([(6, 48), (22, 22), (34, 36), (44, 24), (58, 48)]), fill=(21, 128, 61, 255))
    d.polygon(P([(6, 48), (18, 32), (28, 42), (38, 30), (58, 48)]), fill=(34, 197, 94, 255))

    # White hiking trail / S-Bahn track curve
    d.line(P([(12, 52), (26, 45), (38, 47), (52, 41)]), fill=(255, 255, 255, 255), width=max(2, int(3.5 * s * k)))
    return img


if __name__ == "__main__":
    draw(192).save(os.path.join(HERE, "icon-192.png"), "PNG")
    draw(512).save(os.path.join(HERE, "icon-512.png"), "PNG")
    draw(512, maskable=True).save(os.path.join(HERE, "icon-maskable-512.png"), "PNG")
    print("icons ok")
