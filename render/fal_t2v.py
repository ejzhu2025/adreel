"""fal.ai T2V wrapper — wan/v2.1/t2v-1.3b (turbo)."""
import os
from pathlib import Path

import fal_client
import httpx

_MODEL = os.getenv("FAL_T2V_MODEL", "fal-ai/wan/v2.2-a14b/text-to-video")
_NUM_FRAMES = 81  # always 5s @ 16fps — never generate longer clips


def generate_clip(prompt: str, output_path: str, duration: float = 3.5) -> str:
    """Call T2V (1.3B turbo), download result to output_path. Returns output_path."""
    result = fal_client.run(
        _MODEL,
        arguments={
            "prompt": prompt,
            "num_frames": _NUM_FRAMES,
            "frames_per_second": 16,
            "resolution": "480p",
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
