"""Generate the frosted instrument-panel background for G304 Lite."""

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

W, H = 900, 600
KEY = (0, 0, 1)  # matches KEY in g304lite.py; becomes fully transparent
OUT = Path(__file__).resolve().parent / "assets" / "ui-bg.png"


def main() -> None:
    random.seed(304)
    img = Image.new("RGB", (W, H), (15, 19, 25))

    # Soft light pools, blurred heavily so they read as frosted depth, not glow.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    d.ellipse((560, -140, 1040, 300), fill=(242, 181, 68, 42))
    d.ellipse((-180, 360, 320, 760), fill=(79, 209, 197, 26))
    d.ellipse((300, 180, 620, 500), fill=(90, 120, 170, 18))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")

    d = ImageDraw.Draw(img, "RGBA")
    for x in range(0, W, 30):
        d.line((x, 0, x, H), fill=(255, 255, 255, 5), width=1)
    for y in range(0, H, 30):
        d.line((0, y, W, y), fill=(255, 255, 255, 5), width=1)

    noise = Image.new("L", (W, H))
    noise.putdata([random.randint(116, 140) for _ in range(W * H)])
    noise = noise.point(lambda v: 128 + (v - 128) // 5)
    img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)), 0.035)

    # Round the outer enclosure into the transparency key, then add a restrained
    # bezel highlight.  The native frame is gone, so the frosted surface now
    # reaches the screen edge and the corners are part of the design.
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=18, fill=255)
    img = Image.composite(img, Image.new("RGB", (W, H), KEY), mask)
    ImageDraw.Draw(img, "RGBA").rounded_rectangle(
        (1, 1, W - 2, H - 2), radius=17, outline=(255, 255, 255, 14), width=1)

    OUT.parent.mkdir(exist_ok=True)
    img.save(OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
