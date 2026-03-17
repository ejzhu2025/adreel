"""render/gemini_t2i.py — Ad poster generation via Gemini image generation."""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any


def build_ad_prompt(brand_kit: dict, brief: str = "", cta_text: str = "") -> str:
    """Build a Gemini prompt that turns a product photo into a vertical ad poster."""
    colors = brand_kit.get("colors", {})
    primary = colors.get("primary", "#00B894")
    bg_color = colors.get("background", "#1A1A2E")
    brand_name = brand_kit.get("name", "")

    brief_lower = brief.lower()
    if any(w in brief_lower for w in ["luxury", "premium", "gold", "jewelry", "watch", "perfume"]):
        atmosphere = "quiet luxury aesthetic, dramatic chiaroscuro lighting, deep shadows, single directional light source, dark elegant backdrop"
    elif any(w in brief_lower for w in ["skin", "beauty", "cream", "serum", "glow", "dewy"]):
        atmosphere = "soft warm flattering light, clean minimal background, fresh and luminous mood"
    elif any(w in brief_lower for w in ["summer", "fresh", "cool", "ice", "drink", "beverage"]):
        atmosphere = "refreshing summer vibes, water droplets glistening, cool mist, vibrant saturated colors"
    elif any(w in brief_lower for w in ["sport", "gym", "outdoor", "active", "run"]):
        atmosphere = "high energy, bold dramatic lighting, strong contrast, dynamic atmosphere"
    else:
        atmosphere = "cinematic commercial atmosphere, dramatic studio lighting, clean and premium feel"

    cta_line = f'Include bold call-to-action text "{cta_text}" near the bottom of the image.' if cta_text else ""
    brand_line = f'Place brand name "{brand_name}" elegantly at the top.' if brand_name else ""

    return (
        f"Transform this product photo into a professional vertical 9:16 social media advertisement poster. "
        f"The product must be the clear hero — centered, sharp, and well-lit. "
        f"{atmosphere}. "
        f"Background: rich color palette of {primary} and {bg_color}, smooth gradient, cinematic depth of field. "
        f"{brand_line} "
        f"{cta_line} "
        f"Style: high-end commercial photography, no watermarks, no extra people, photorealistic, "
        f"magazine-quality advertisement. Output exactly one complete poster image."
    )


def generate_ad_frame(
    product_image_path: str,
    prompt: str,
    output_path: str,
    gemini_client: Any,
) -> str:
    """Generate an ad poster from a product photo using Gemini image generation.

    Sends the product image + prompt to Gemini, gets back a generated poster image.
    """
    from google.genai import types

    img_bytes = Path(product_image_path).read_bytes()
    ext = Path(product_image_path).suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    b64 = base64.b64encode(img_bytes).decode()

    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=[
            {
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": b64}},
                    {"text": prompt},
                ]
            }
        ],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            temperature=0.7,
        ),
    )

    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data:
            raw = part.inline_data.data
            if isinstance(raw, str):
                raw = base64.b64decode(raw)
            Path(output_path).write_bytes(raw)
            return output_path

    raise RuntimeError("Gemini returned no image for ad poster generation")


def build_scene_prompt(scene_desc: str, style_tone: list[str] | None = None) -> str:
    """Build a prompt that places the product into a specific scene context."""
    tone_str = ", ".join(style_tone) if style_tone else "cinematic, premium"
    return (
        f"Using this exact product as the hero, create a photorealistic vertical 9:16 scene: {scene_desc}. "
        f"The product must appear exactly as shown in the reference photo — same shape, color, and details. "
        f"Style: {tone_str}. Cinematic lighting, high-end commercial photography quality. "
        f"No text, no logos, no watermarks. Output exactly one complete image."
    )


def generate_scene_frame(
    product_image_path: str,
    scene_desc: str,
    output_path: str,
    gemini_client: Any,
    style_tone: list[str] | None = None,
) -> str:
    """Generate a scene image with the product as hero, using the product photo as reference.

    Uses gemini-2.5-flash-image with the product image as input,
    so the generated frame shows the exact same product (consistent shape/color/details).
    """
    from google.genai import types

    img_bytes = Path(product_image_path).read_bytes()
    ext = Path(product_image_path).suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    b64 = base64.b64encode(img_bytes).decode()

    prompt = build_scene_prompt(scene_desc, style_tone)

    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=[
            {
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": b64}},
                    {"text": prompt},
                ]
            }
        ],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            temperature=0.6,
        ),
    )

    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data:
            raw = part.inline_data.data
            if isinstance(raw, str):
                raw = base64.b64decode(raw)
            Path(output_path).write_bytes(raw)
            return output_path

    raise RuntimeError("Gemini returned no image for scene frame generation")
