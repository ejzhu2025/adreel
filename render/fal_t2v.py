"""fal.ai T2V wrapper — supports turbo (1.3B) and hd (14B) quality tiers."""
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

import fal_client
import httpx

# fal_client.run() has no built-in timeout — wrap all calls with this limit (seconds)
FAL_CALL_TIMEOUT = 300  # 5 minutes per clip

# ── Prompt sanitiser — prevent content-policy false-positives ─────────────────
# fal.ai's content checker flags food/cooking words that look violent out of context.
_SANITIZE_RULES = [
    # "flesh" in food context → "pulp" / "fruit interior"
    (r"\b(vivid\s+\w+\s+)?flesh\b", "pulp"),
    # "blade" near food → "knife edge"
    (r"\bblade\s+(slicing|cutting|entering)\b", "knife cutting"),
    (r"\bslicing\s+through\b", "cutting through"),
    # "burst" of juice can look violent — soften
    (r"\bjuice\s+droplets\s+burst\b", "juice droplets spray"),
    # "crack open" / "cracks open"
    (r"\bcracks?\s+open\b", "splits open"),
    # "wipe" as a film transition is fine, but "split-screen wipe cracks" is flagged
    (r"\bsplit-screen\s+wipe\s+cracks\b", "split-screen wipe reveals"),
]


def _sanitize_prompt(prompt: str) -> str:
    """Replace known content-policy trigger patterns with food-safe equivalents."""
    for pattern, replacement in _SANITIZE_RULES:
        prompt = re.sub(pattern, replacement, prompt, flags=re.IGNORECASE)
    return prompt

# Quality presets
_QUALITY_PRESETS = {
    "turbo": {
        "model": os.getenv("FAL_T2V_MODEL", "fal-ai/wan/v2.2-a14b/text-to-video"),
        "num_frames": 33,   # ~2s @ 16fps — fast/cheap preview (min supported: 17)
        "resolution": "480p",
        "api": "wan",
    },
    "hd": {
        "model": os.getenv("FAL_T2V_MODEL", "fal-ai/wan/v2.2-a14b/text-to-video"),
        "num_frames": 81,   # ~5s @ 16fps — full quality
        "resolution": "720p",
        "api": "wan",
    },
    # Kling — best quality, used for marketing batch tasks (no time urgency)
    "kling": {
        "model": "fal-ai/kling-video/v1.6/standard/text-to-video",
        "duration": "5",    # "5" or "10" seconds
        "aspect_ratio": "9:16",
        "api": "kling",
    },
}


_DEFAULT_NEGATIVE = (
    "text on screen, captions, subtitles, watermark, logo, blurry, low quality, "
    "distorted proportions, unnatural motion, CGI artifacts, overexposed, flat lighting"
)


def generate_clip(
    prompt: str,
    output_path: str,
    duration: float = 3.5,
    quality: str = "turbo",
    negative_prompt: str = "",
) -> str:
    """Call T2V, download result to output_path. quality='turbo'|'hd'."""
    preset = _QUALITY_PRESETS.get(quality, _QUALITY_PRESETS["turbo"])
    neg = negative_prompt or _DEFAULT_NEGATIVE

    # Always sanitise before sending
    clean_prompt = _sanitize_prompt(prompt)

    url = _call_t2v(preset, clean_prompt, neg)
    if url is None:
        # Retry once with a stripped-down fallback prompt on content policy errors
        fallback = _make_fallback_prompt(clean_prompt)
        print(f"[fal_t2v] Retrying with fallback prompt: {fallback[:80]}…")
        url = _call_t2v(preset, fallback, neg, reraise=True)

    with httpx.Client(timeout=180, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    return output_path


def _call_t2v(preset: dict, prompt: str, neg: str, reraise: bool = False) -> str | None:
    """Single T2V call. Returns URL or None on content_policy_violation."""
    def _run():
        if preset.get("api") == "kling":
            return fal_client.run(
                preset["model"],
                arguments={
                    "prompt": prompt,
                    "negative_prompt": neg,
                    "duration": preset.get("duration", "5"),
                    "aspect_ratio": preset.get("aspect_ratio", "9:16"),
                    "cfg_scale": 0.5,
                },
            )
        return fal_client.run(
            preset["model"],
            arguments={
                "prompt": prompt,
                "negative_prompt": neg,
                "num_frames": preset["num_frames"],
                "frames_per_second": 16,
                "resolution": preset["resolution"],
                "aspect_ratio": "9:16",
            },
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_run)
            try:
                result = future.result(timeout=FAL_CALL_TIMEOUT)
            except FuturesTimeoutError:
                raise TimeoutError(
                    f"fal.ai T2V timed out after {FAL_CALL_TIMEOUT}s — "
                    "queue may be overloaded, try again later"
                )
        if "video" in result:
            return result["video"]["url"]
        if "videos" in result and result["videos"]:
            return result["videos"][0]["url"]
        raise ValueError(f"Unexpected T2V response: {list(result.keys())}")
    except Exception as e:
        err_str = str(e)
        # Extract video URL from interpolation errors (fal sometimes errors after generating)
        match = re.search(r'https://fal\.media/files/[^\s\'"]+\.mp4', err_str)
        if match:
            return match.group(0)
        if "content_policy_violation" in err_str:
            if reraise:
                raise
            return None  # caller will retry with fallback
        raise


def _make_fallback_prompt(prompt: str) -> str:
    """Strip the first sentence (usually the most descriptive / risky) and keep the style tail."""
    sentences = [s.strip() for s in prompt.split(".") if s.strip()]
    # Drop the first sentence (subject action), keep style + safety sentences
    safe = ". ".join(sentences[1:]) if len(sentences) > 1 else prompt
    # Ensure it still has a basic subject
    return f"Product shot. {safe}"
