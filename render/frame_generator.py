"""frame_generator — create branded placeholder frames with PIL."""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# Palette presets per shot type
_TYPE_GRADIENTS = {
    "macro":      [("#00B894", "#00CEC9")],
    "product":    [("#6C5CE7", "#A29BFE")],
    "lifestyle":  [("#FD79A8", "#E17055")],
    "close":      [("#00B894", "#55EFC4")],
    "wide":       [("#0984E3", "#74B9FF")],
    "text":       [("#2D3436", "#636E72")],
    "transition": [("#1A1A2E", "#16213E")],
}


class FrameGenerator:
    def __init__(self, brand_kit: dict[str, Any], work_dir: Path):
        self.brand_kit = brand_kit
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.colors = brand_kit.get("colors", {})
        self.subtitle_style = brand_kit.get("subtitle_style", {})
        self._font_cache: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}

    def _get_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if size in self._font_cache:
            return self._font_cache[size]
        # Try system fonts (macOS / Linux)
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, size)
                    self._font_cache[size] = font
                    return font
                except Exception:
                    continue
        font = ImageFont.load_default()
        self._font_cache[size] = font
        return font

    def generate_frame(
        self,
        shot_id: str,
        shot_type: str = "wide",
        text_overlay: str = "",
        scene_index: int = 0,
        is_intro: bool = False,
        is_outro: bool = False,
        background_image_path: str = "",
        logo_path: str = "",
    ) -> Path:
        W, H = 1080, 1920

        if background_image_path and os.path.exists(background_image_path):
            # Use AI-generated background — open, resize to 9:16
            img = Image.open(background_image_path).convert("RGB").resize((W, H))
            draw = ImageDraw.Draw(img, "RGBA")
            # Subtle dark vignette at edges so text pops
            self._draw_vignette(draw, W, H)
        else:
            img = Image.new("RGB", (W, H), color="#1A1A2E")
            draw = ImageDraw.Draw(img, "RGBA")
            # Background gradient + decorative shape (PIL-only mode)
            self._draw_gradient(draw, W, H, shot_type, scene_index)
            self._draw_decor(draw, W, H, shot_type, scene_index)

        # Outro: embed logo image centered at top third
        if is_outro and logo_path and os.path.exists(logo_path):
            self._draw_logo(img, W, H, logo_path)

        # Main text overlay
        if text_overlay:
            self._draw_text_block(draw, W, H, text_overlay, is_cta=is_outro)

        out_path = self.work_dir / f"{shot_id}_frame.png"
        img.save(str(out_path), "PNG")
        return out_path

    # ── Private helpers ───────────────────────────────────────────────────────

    def generate_brand_overlay(
        self,
        output_path: str,
        logo_path: str,
        cta_text: str,
        W: int = 1080,
        H: int = 1920,
    ) -> str:
        """Generate a transparent RGBA PNG with logo + CTA text for FFmpeg overlay.

        The overlay is designed to sit on top of an I2V video:
        - Logo centered at upper-third (y ≈ 28%)
        - CTA text centered at lower quarter (y ≈ 75%) with semi-transparent box
        """
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # fully transparent
        draw = ImageDraw.Draw(img, "RGBA")

        # ── Logo ──────────────────────────────────────────────────────────────
        if logo_path and os.path.exists(logo_path):
            try:
                logo = Image.open(logo_path).convert("RGBA")
                max_w = 300
                ratio = min(max_w / logo.width, max_w / logo.height)
                lw, lh = int(logo.width * ratio), int(logo.height * ratio)
                logo = logo.resize((lw, lh), Image.LANCZOS)
                lx = (W - lw) // 2
                ly = int(H * 0.28) - lh // 2
                img.paste(logo, (lx, ly), logo)
            except Exception:
                pass  # skip logo if it can't be loaded

        # ── CTA text box ──────────────────────────────────────────────────────
        if cta_text:
            sub_style = self.subtitle_style
            font_size = sub_style.get("font_size", 52)
            box_opacity = int(sub_style.get("box_opacity", 0.65) * 255)
            padding = sub_style.get("padding_px", 18)
            box_radius = sub_style.get("box_radius", 14)

            font = self._get_font(font_size)
            lines = cta_text.split("\n")
            line_widths, line_heights = [], []
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                line_widths.append(bbox[2] - bbox[0])
                line_heights.append(bbox[3] - bbox[1])

            block_w = max(line_widths) + padding * 2
            block_h = sum(line_heights) + padding * 2 + (len(lines) - 1) * 8
            box_x = (W - block_w) // 2
            box_y = int(H * 0.75) - block_h // 2

            _draw_rounded_rect(draw, box_x, box_y, block_w, block_h, box_radius,
                               (0, 0, 0, box_opacity))

            accent = _hex_to_rgb(self.colors.get("accent", "#FF7675"))
            y_cursor = box_y + padding
            for i, line in enumerate(lines):
                color = (*accent, 255) if i == len(lines) - 1 else (255, 255, 255, 255)
                x = box_x + (block_w - line_widths[i]) // 2
                draw.text((x, y_cursor), line, font=font, fill=color)
                y_cursor += line_heights[i] + 8

        img.save(output_path, "PNG")
        return output_path

    def _draw_vignette(self, draw: ImageDraw.ImageDraw, W: int, H: int) -> None:
        """Soft dark vignette around edges so overlaid text is readable."""
        steps = 80
        for i in range(steps):
            alpha = int(120 * (i / steps) ** 2)
            margin = i * 6
            draw.rectangle(
                [(margin, margin), (W - margin, H - margin)],
                outline=(0, 0, 0, alpha),
                width=6,
            )

    def _draw_logo(self, img: Image.Image, W: int, H: int, logo_path: str) -> None:
        """Embed logo centered at the upper third of the frame."""
        try:
            logo = Image.open(logo_path).convert("RGBA")
            # Scale to max 320px wide, keep aspect ratio
            max_w = 320
            ratio = min(max_w / logo.width, max_w / logo.height)
            lw = int(logo.width * ratio)
            lh = int(logo.height * ratio)
            logo = logo.resize((lw, lh), Image.LANCZOS)
            x = (W - lw) // 2
            y = int(H * 0.28) - lh // 2  # upper-third center
            img.paste(logo, (x, y), logo)
        except Exception:
            pass  # silently skip if logo can't be loaded

    def _draw_gradient(self, draw: ImageDraw.ImageDraw, W: int, H: int, shot_type: str, idx: int) -> None:
        gradients = _TYPE_GRADIENTS.get(shot_type, _TYPE_GRADIENTS["wide"])
        top_hex, bot_hex = gradients[0]

        # text/transition shots always use brand colors (never fall back to gray palette)
        if shot_type in ("text", "transition"):
            top = _hex_to_rgb(self.colors.get("primary", top_hex))
            bot = _hex_to_rgb(self.colors.get("background", bot_hex))
        else:
            top = _hex_to_rgb(self.colors.get("primary", top_hex))
            bot = _hex_to_rgb(self.colors.get("background", bot_hex))
            # Alternate palette on odd-indexed shots for visual variety
            if idx % 2 == 1:
                top = _hex_to_rgb(top_hex)
                bot = _hex_to_rgb(bot_hex)

        for y in range(H):
            r = int(top[0] + (bot[0] - top[0]) * y / H)
            g = int(top[1] + (bot[1] - top[1]) * y / H)
            b = int(top[2] + (bot[2] - top[2]) * y / H)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

    def _draw_decor(self, draw: ImageDraw.ImageDraw, W: int, H: int, shot_type: str, idx: int) -> None:
        accent = _hex_to_rgb(self.colors.get("accent", "#FF7675"))
        cx, cy = W // 2, H // 2

        if shot_type in ("macro", "close"):
            # Large translucent circle center
            r = 320
            draw.ellipse(
                [(cx - r, cy - r - 80), (cx + r, cy + r - 80)],
                fill=(*accent, 35),
                outline=(*accent, 80),
                width=3,
            )
        elif shot_type == "product":
            # Ring
            for ring_r in [280, 340, 400]:
                draw.ellipse(
                    [(cx - ring_r, cy - ring_r - 60), (cx + ring_r, cy + ring_r - 60)],
                    outline=(*accent, 50),
                    width=2,
                )
        elif shot_type == "text":
            # Horizontal bars
            secondary = _hex_to_rgb(self.colors.get("secondary", "#FFFFFF"))
            for bar_y in [H // 3, 2 * H // 3]:
                draw.rectangle([(0, bar_y - 2), (W, bar_y + 2)], fill=(*secondary, 40))
        else:
            # Diagonal lines
            secondary = _hex_to_rgb(self.colors.get("secondary", "#FFFFFF"))
            for i in range(-10, 20):
                offset = i * 150
                draw.line(
                    [(offset, 0), (offset + H, H)],
                    fill=(*secondary, 15),
                    width=1,
                )

    def _draw_text_block(
        self,
        draw: ImageDraw.ImageDraw,
        W: int,
        H: int,
        text: str,
        is_cta: bool = False,
    ) -> None:
        sub_style = self.subtitle_style
        font_size = sub_style.get("font_size", 52)
        box_opacity = int(sub_style.get("box_opacity", 0.55) * 255)
        padding = sub_style.get("padding_px", 14)
        box_radius = sub_style.get("box_radius", 12)

        font = self._get_font(font_size if not is_cta else font_size + 8)
        lines = text.split("\n")
        line_heights = []
        line_widths = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_widths.append(bbox[2] - bbox[0])
            line_heights.append(bbox[3] - bbox[1])

        block_w = max(line_widths) + padding * 2
        block_h = sum(line_heights) + padding * 2 + (len(lines) - 1) * 8

        # Position: bottom_center (default) or center for CTA
        if is_cta or sub_style.get("position") == "center":
            box_x = (W - block_w) // 2
            box_y = H // 2 - block_h // 2
        else:
            box_x = (W - block_w) // 2
            box_y = H - block_h - 180  # safe area bottom

        # Draw rounded background box
        box_color = (0, 0, 0, box_opacity)
        _draw_rounded_rect(draw, box_x, box_y, block_w, block_h, box_radius, box_color)

        # Draw text lines
        secondary = _hex_to_rgb(self.colors.get("secondary", "#FFFFFF"))
        accent = _hex_to_rgb(self.colors.get("accent", "#FF7675"))
        y_cursor = box_y + padding
        for i, line in enumerate(lines):
            # Highlight last line for CTA
            color = (*accent, 255) if is_cta and i == len(lines) - 1 else (*secondary, 255)
            x = box_x + (block_w - line_widths[i]) // 2
            draw.text((x, y_cursor), line, font=font, fill=color)
            y_cursor += line_heights[i] + 8


# ── Utility ───────────────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


def _draw_rounded_rect(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    radius: int,
    fill: tuple,
) -> None:
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill)
