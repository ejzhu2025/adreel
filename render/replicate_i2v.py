"""Replicate I2V wrapper — Wan 2.2 fast image-to-video."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import replicate

_MODEL = os.getenv("REPLICATE_I2V_MODEL", "wan-video/wan-2.2-i2v-fast")

_QUALITY_PRESETS = {
    "turbo": {"num_frames": 33, "resolution": "480p"},
    "hd":    {"num_frames": 65, "resolution": "720p"},
}


def _extract_url(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list) and output:
        item = output[0]
        return item.url if hasattr(item, "url") else str(item)
    if hasattr(output, "url"):
        return output.url
    raise ValueError(f"Unexpected Replicate I2V output: {type(output)}")


def generate_clip_from_image(
    image_path: str,
    motion_prompt: str,
    output_path: str,
    quality: str = "turbo",
) -> str:
    """Animate a product photo via Replicate Wan 2.2 I2V. Returns output_path."""
    preset = _QUALITY_PRESETS.get(quality, _QUALITY_PRESETS["turbo"])

    with open(image_path, "rb") as img_file:
        output = replicate.run(
            _MODEL,
            input={
                "image": img_file,
                "prompt": motion_prompt,
                "num_frames": preset["num_frames"],
                "fps": 16,
                "resolution": preset["resolution"],
                "aspect_ratio": "9:16",
            },
        )

    url = _extract_url(output)
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    return output_path


# Re-export prompt builders from fal_i2v (logic is model-agnostic)
from render.fal_i2v import build_shot_motion_prompt, build_outro_motion_prompt  # noqa: E402, F401
