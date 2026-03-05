"""fal.ai I2V wrapper — animate a product photo into a short video clip."""
from __future__ import annotations

from pathlib import Path

import fal_client
import httpx

# Quality presets matching T2V conventions
_PRESETS = {
    "turbo": {"num_frames": 33, "resolution": "480p"},
    "hd":    {"num_frames": 65, "resolution": "720p"},
}


def generate_clip_from_image(
    image_path: str,
    motion_prompt: str,
    output_path: str,
    quality: str = "turbo",
) -> str:
    """Animate a product photo via Wan I2V. Returns output_path."""
    preset = _PRESETS.get(quality, _PRESETS["turbo"])

    # Upload local image to fal.ai CDN so the API can access it
    image_url = fal_client.upload_file(image_path)

    result = fal_client.run(
        "fal-ai/wan/v2.2-a14b/image-to-video",
        arguments={
            "image_url": image_url,
            "prompt": motion_prompt,
            "num_frames": preset["num_frames"],
            "frames_per_second": 16,
            "resolution": preset["resolution"],
            "aspect_ratio": "9:16",
        },
    )

    if "video" in result:
        url = result["video"]["url"]
    elif "videos" in result and result["videos"]:
        url = result["videos"][0]["url"]
    else:
        raise ValueError(f"Unexpected I2V response keys: {list(result.keys())}")

    with httpx.Client(timeout=180, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    return output_path


def build_outro_motion_prompt(brand_kit: dict, brief: str = "") -> str:
    """Motion-focused prompt for I2V outro — describes camera/motion only, not the subject."""
    brief_lower = brief.lower()
    if any(w in brief_lower for w in ["summer", "fresh", "cool", "ice", "watermelon"]):
        mood = "cool refreshing mist, water droplets catching light"
    elif any(w in brief_lower for w in ["luxury", "premium", "gold"]):
        mood = "dramatic golden rim lighting, luxury atmosphere"
    else:
        mood = "soft bokeh background, cinematic atmosphere"

    return (
        f"Slow cinematic product reveal, product gently rotating in place, "
        f"{mood}, dramatic studio lighting from the side, "
        f"product perfectly centered and sharp, smooth slow motion, "
        f"no camera movement, no people, no text, no logos"
    )
