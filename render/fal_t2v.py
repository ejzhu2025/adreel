"""fal.ai T2V wrapper — supports turbo (1.3B) and hd (14B) quality tiers."""
import os
from pathlib import Path

import fal_client
import httpx

# Quality presets — both use wan/v2.2-a14b, turbo uses fewer frames for speed
_QUALITY_PRESETS = {
    "turbo": {
        "model": os.getenv("FAL_T2V_MODEL", "fal-ai/wan/v2.2-a14b/text-to-video"),
        "num_frames": 33,   # ~2s @ 16fps — fast/cheap preview (min supported: 17)
        "resolution": "480p",
    },
    "hd": {
        "model": os.getenv("FAL_T2V_MODEL", "fal-ai/wan/v2.2-a14b/text-to-video"),
        "num_frames": 81,   # ~5s @ 16fps — full quality
        "resolution": "720p",
    },
}


def generate_clip(prompt: str, output_path: str, duration: float = 3.5, quality: str = "turbo") -> str:
    """Call T2V, download result to output_path. quality='turbo'|'hd'."""
    preset = _QUALITY_PRESETS.get(quality, _QUALITY_PRESETS["turbo"])
    result = fal_client.run(
        preset["model"],
        arguments={
            "prompt": prompt,
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
        raise ValueError(f"Unexpected T2V response: {list(result.keys())}")

    with httpx.Client(timeout=180, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    return output_path
