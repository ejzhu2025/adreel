"""fal.ai I2V wrapper — animate a product photo into a short video clip."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

import fal_client
import httpx

FAL_CALL_TIMEOUT = 300  # 5 minutes

# Quality presets matching T2V conventions
_PRESETS = {
    "turbo": {"model": "fal-ai/wan/v2.2-a14b/image-to-video", "num_frames": 33, "resolution": "480p", "api": "wan"},
    "hd":    {"model": "fal-ai/wan/v2.2-a14b/image-to-video", "num_frames": 65, "resolution": "720p", "api": "wan"},
    "kling": {"model": "fal-ai/kling-video/v1.6/standard/image-to-video", "duration": "5", "aspect_ratio": "9:16", "api": "kling"},
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

    def _run():
        if preset.get("api") == "kling":
            return fal_client.run(
                preset["model"],
                arguments={
                    "image_url": image_url,
                    "prompt": motion_prompt,
                    "duration": preset.get("duration", "5"),
                    "aspect_ratio": preset.get("aspect_ratio", "9:16"),
                    "cfg_scale": 0.5,
                },
            )
        return fal_client.run(
            preset["model"],
            arguments={
                "image_url": image_url,
                "prompt": motion_prompt,
                "num_frames": preset["num_frames"],
                "frames_per_second": 16,
                "resolution": preset["resolution"],
                "aspect_ratio": "9:16",
            },
        )

    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_run)
        try:
            result = future.result(timeout=FAL_CALL_TIMEOUT)
        except FuturesTimeoutError:
            raise TimeoutError(
                f"fal.ai I2V timed out after {FAL_CALL_TIMEOUT}s"
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


def build_shot_motion_prompt(shot_type: str, scene_desc: str, brief: str = "") -> str:
    """Motion-focused I2V prompt for product/lifestyle shots.

    Describes only camera movement and atmosphere — never the product itself —
    so the I2V model animates the uploaded photo without inventing text or logos.
    """
    brief_lower = brief.lower()
    if any(w in brief_lower for w in ["summer", "fresh", "cool", "watermelon"]):
        light = "soft natural daylight, cool refreshing atmosphere"
    elif any(w in brief_lower for w in ["luxury", "premium", "gold"]):
        light = "dramatic rim lighting, luxury cinematic atmosphere"
    else:
        light = "soft studio lighting, cinematic bokeh"

    if shot_type == "lifestyle":
        return (
            f"Gentle handheld camera movement, slow tilt upward, {light}, "
            "smooth slow motion, no text, no logos, no watermarks"
        )
    # product, macro, close, wide
    return (
        f"Slow cinematic product reveal, gentle rotation, {light}, "
        "product sharp and centered, smooth slow motion, no text, no logos, no watermarks"
    )


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
