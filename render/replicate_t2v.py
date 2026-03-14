"""Replicate T2V wrapper — Wan 2.2 fast text-to-video."""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import replicate

REPLICATE_CALL_TIMEOUT = 300  # 5 minutes

# Model IDs — override via env vars if needed
_MODELS = {
    "turbo": os.getenv("REPLICATE_T2V_MODEL", "wan-video/wan-2.2-t2v-fast"),
    "hd":    os.getenv("REPLICATE_T2V_MODEL_HD", "wan-video/wan-2.2-t2v-fast"),
}

_QUALITY_PRESETS = {
    "turbo": {"num_frames": 33, "resolution": "480p"},
    "hd":    {"num_frames": 81, "resolution": "720p"},
}

_DEFAULT_NEGATIVE = (
    "text on screen, captions, subtitles, watermark, logo, blurry, low quality, "
    "distorted proportions, unnatural motion, CGI artifacts, overexposed, flat lighting"
)

# Prompt sanitiser — same rules as fal_t2v to prevent content-policy issues
_SANITIZE_RULES = [
    (r"\b(vivid\s+\w+\s+)?flesh\b", "pulp"),
    (r"\bblade\s+(slicing|cutting|entering)\b", "knife cutting"),
    (r"\bslicing\s+through\b", "cutting through"),
    (r"\bjuice\s+droplets\s+burst\b", "juice droplets spray"),
    (r"\bcracks?\s+open\b", "splits open"),
]


def _sanitize(prompt: str) -> str:
    for pattern, replacement in _SANITIZE_RULES:
        prompt = re.sub(pattern, replacement, prompt, flags=re.IGNORECASE)
    return prompt


def _extract_url(output) -> str:
    """Handle various Replicate output formats → return a downloadable URL."""
    if isinstance(output, str):
        return output
    if isinstance(output, list) and output:
        item = output[0]
        return item.url if hasattr(item, "url") else str(item)
    if hasattr(output, "url"):
        return output.url
    raise ValueError(f"Unexpected Replicate output type: {type(output)}, value: {output!r}")


def generate_clip(
    prompt: str,
    output_path: str,
    duration: float = 3.5,
    quality: str = "turbo",
    negative_prompt: str = "",
) -> str:
    """Call Replicate Wan 2.2 T2V, download result to output_path."""
    preset = _QUALITY_PRESETS.get(quality, _QUALITY_PRESETS["turbo"])
    model = _MODELS.get(quality, _MODELS["turbo"])
    neg = negative_prompt or _DEFAULT_NEGATIVE
    clean_prompt = _sanitize(prompt)

    output = replicate.run(
        model,
        input={
            "prompt": clean_prompt,
            "negative_prompt": neg,
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
