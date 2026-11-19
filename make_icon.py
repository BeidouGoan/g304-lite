"""Generate the G304 Lite icon set (ICO for window/tray, PNG preview)."""

from pathlib import Path
import io
import struct

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent / "assets"
ASSETS.mkdir(exist_ok=True)
AMBER = (186, 117, 23, 255)
WHITE = (255, 255, 255, 255)


def draw_icon(size: int, simple: bool = False) -> Image.Image:
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(s * 0.02)
    d.rounded_rectangle((pad, pad, s - pad, s - pad), radius=int(s * 0.22), fill=AMBER)

    if simple:
        # 16/24px: keep the silhouette and two bars only.
        d.rounded_rectangle((int(s * 0.31), int(s * 0.20), int(s * 0.69), int(s * 0.72)),
                            radius=int(s * 0.18), fill=WHITE)
        d.rounded_rectangle((int(s * 0.47), int(s * 0.24), int(s * 0.53), int(s * 0.34)),
                            radius=int(s * 0.03), fill=AMBER)
        d.line((s // 2, int(s * 0.38), s // 2, int(s * 0.68)), fill=AMBER, width=max(1, s // 64))
        for i, alpha in enumerate((255, 140)):
            x0 = int(s * (0.31 + i * 0.20))
            d.rounded_rectangle((x0, int(s * 0.78), x0 + int(s * 0.15), int(s * 0.87)),
                                radius=int(s * 0.02), fill=(255, 255, 255, alpha))
    else:
        d.rounded_rectangle((int(s * 0.31), int(s * 0.19), int(s * 0.69), int(s * 0.71)),
                            radius=int(s * 0.19), fill=WHITE)
        d.rounded_rectangle((int(s * 0.475), int(s * 0.235), int(s * 0.525), int(s * 0.335)),
                            radius=int(s * 0.025), fill=AMBER)
        d.line((s // 2, int(s * 0.37), s // 2, int(s * 0.67)), fill=AMBER, width=max(1, s // 80))
        for i, alpha in enumerate((255, 205, 150, 95)):
            x0 = int(s * (0.265 + i * 0.127))
            d.rounded_rectangle((x0, int(s * 0.775), x0 + int(s * 0.095), int(s * 0.87)),
                                radius=int(s * 0.018), fill=(255, 255, 255, alpha))

    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """Write a multi-size ICO with PNG-compressed entries (Vista+ compatible).

    Pillow's own ICO writer only emits sizes <= the base image, and would
    upscale our deliberately simplified 16px drawing.  Building the directory
    by hand keeps each hand-tuned size intact.
    """
    header = io.BytesIO()
    header.write(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    entries = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        entries.append((data, offset))
        offset += len(data)
    for img, (data, data_offset) in zip(images, entries):
        w, h = img.size
        header.write(struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(data), data_offset))
    path.write_bytes(header.getvalue() + b"".join(data for data, _ in entries))


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw_icon(n, simple=n <= 24) for n in sizes]
    ico_path = ASSETS / "g304-lite.ico"
    write_ico(ico_path, images)
    png_path = ASSETS / "g304-lite.png"
    draw_icon(256).save(png_path)
    draw_icon(24).save(ASSETS / "g304-lite-24.png")
    print(f"wrote {ico_path} ({ico_path.stat().st_size} bytes)")
    print(f"wrote {png_path} ({png_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
