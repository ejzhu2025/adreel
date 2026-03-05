"""fal.ai T2I wrapper — FLUX Schnell background + FLUX Kontext product ad frames."""
from __future__ import annotations

from pathlib import Path

import fal_client
import httpx


def generate_ad_frame(
    product_image_path: str,
    prompt: str,
    output_path: str,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Generate a luxury ad-style image from a product photo via FLUX Kontext.

    FLUX Kontext preserves the product's appearance while transforming the
    scene, lighting and atmosphere into a professional advertisement.
    """
    # Upload product image to fal.ai CDN so the API can access it
    image_url = fal_client.upload_file(product_image_path)

    result = fal_client.run(
        "fal-ai/flux-pro/kontext",
        arguments={
            "prompt": prompt,
            "image_url": image_url,
            "image_size": {"width": width, "height": height},
            "num_inference_steps": 28,
            "guidance_scale": 3.5,
            "output_format": "png",
        },
    )
    url = result["images"][0]["url"]
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    return output_path


def build_ad_prompt(brand_kit: dict, brief: str = "", cta_text: str = "") -> str:
    """Build a FLUX Kontext prompt that turns a product photo into a complete ad poster.

    The prompt asks the model to integrate CTA text and brand identity directly
    into the image — no post-compositing overlay needed.
    """
    colors = brand_kit.get("colors", {})
    primary = colors.get("primary", "#00B894")
    bg_color = colors.get("background", "#1A1A2E")
    brand_name = brand_kit.get("name", "")

    brief_lower = brief.lower()
    if any(w in brief_lower for w in ["summer", "fresh", "cool", "ice", "watermelon"]):
        atmosphere = "refreshing summer vibes, water droplets glistening, cool mist, vibrant greens"
    elif any(w in brief_lower for w in ["luxury", "premium", "gold"]):
        atmosphere = "luxury premium atmosphere, golden rim light, dark elegant backdrop"
    elif any(w in brief_lower for w in ["nature", "organic", "natural"]):
        atmosphere = "natural organic atmosphere, soft green botanicals, clean light"
    else:
        atmosphere = "cinematic commercial atmosphere, dramatic studio lighting"

    cta_line = f'bold call-to-action text "{cta_text}" near the bottom' if cta_text else ""
    brand_line = f'brand name "{brand_name}" elegantly placed at the top' if brand_name else ""

    return (
        f"Transform this into a full vertical 9:16 social media advertisement poster. "
        f"The product is the hero, centered and sharp. "
        f"{atmosphere}. "
        f"Background: deep rich color palette of {primary} and {bg_color}, "
        f"smooth gradient, cinematic depth of field. "
        f"{brand_line}. "
        f"{cta_line}. "
        f"High-end commercial photography style, dramatic backlighting, "
        f"clean elegant typography integrated into the scene, "
        f"no watermarks, no people, photorealistic"
    )


def generate_background(
    prompt: str,
    output_path: str,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Generate a background image via FLUX Schnell. Returns output_path."""
    result = fal_client.run(
        "fal-ai/flux/schnell",
        arguments={
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_inference_steps": 4,
            "num_images": 1,
            "enable_safety_checker": False,
        },
    )
    url = result["images"][0]["url"]
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    return output_path


def build_background_prompt(brand_kit: dict, is_outro: bool = False) -> str:
    """Generate a FLUX prompt for a branded background based on brand colors."""
    colors = brand_kit.get("colors", {})
    primary = colors.get("primary", "#00B894")
    bg_color = colors.get("background", "#1A1A2E")

    if is_outro:
        return (
            f"Elegant minimal abstract background, dominant colors {primary} and {bg_color}, "
            "soft bokeh light particles, smooth color gradient, cinematic depth of field, "
            "luxury brand aesthetic, no text, no people, no logos, no watermarks, "
            "dark moody atmosphere, subtle geometric shapes, professional photography"
        )
    return (
        f"Abstract minimal background, colors {primary} and {bg_color}, "
        "soft light rays, geometric shapes, clean modern aesthetic, "
        "no text, no people, no logos, studio quality"
    )
