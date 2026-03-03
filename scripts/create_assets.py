"""Generate placeholder brand assets using PIL."""
from __future__ import annotations

from pathlib import Path


def create_placeholder_logo(
    output_path: str | Path = "assets/tong_sui_logo.png",
    size: tuple[int, int] = (240, 80),
) -> Path:
    """Create a simple branded logo placeholder with PIL."""
    from PIL import Image, ImageDraw, ImageFont
    import os

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    W, H = size
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background pill shape
    bg_color = (0, 184, 148, 220)  # #00B894 with alpha
    draw.rounded_rectangle([0, 0, W - 1, H - 1], radius=H // 2, fill=bg_color)

    # Text "TONG SUI"
    text = "TONG SUI"
    font = _get_font(24)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (W - tw) // 2
    ty = (H - th) // 2
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))

    # Small circle decoration
    cr = H // 5
    draw.ellipse([W - cr * 2 - 8, H // 2 - cr, W - 8, H // 2 + cr], fill=(255, 118, 117, 200))

    img.save(str(out), "PNG")
    return out


def _get_font(size: int):
    from PIL import ImageFont
    import os

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


if __name__ == "__main__":
    path = create_placeholder_logo()
    print(f"Logo created: {path}")
