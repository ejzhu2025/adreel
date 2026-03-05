"""music_mixer — generate background music via Replicate MusicGen and mix into video."""
from __future__ import annotations

import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from rich.console import Console

from render.ffmpeg_composer import FFmpegComposer, _probe_duration

console = Console()

# Keyword → tone mapping for auto-selection
_TONE_MAP: dict[str, list[str]] = {
    "upbeat":   ["energetic", "fun", "fast", "exciting", "fitness", "dance", "sport", "dynamic", "vibrant", "lively"],
    "warm":     ["cozy", "warm", "comfort", "food", "dessert", "coffee", "homey", "fresh", "playful", "sweet"],
    "chill":    ["calm", "relax", "lifestyle", "travel", "wellness", "skincare", "peaceful", "minimal", "clean", "soft"],
    "dramatic": ["bold", "premium", "luxury", "launch", "brand", "impact", "power", "cinematic", "epic", "intense"],
}

_TONE_PROMPTS: dict[str, str] = {
    "upbeat":   "energetic upbeat electronic instrumental background music, fast tempo, no vocals",
    "warm":     "warm cozy acoustic instrumental background music, gentle guitar, no vocals",
    "chill":    "calm relaxing lo-fi instrumental background music, ambient, no vocals",
    "dramatic": "bold cinematic orchestral instrumental background music, epic, no vocals",
}

# Replicate MusicGen model
_REPLICATE_MODEL = "meta/musicgen:671ac645ce5e552cc63a54a2bbff63fcf798043055d2dac5fc9e36a837eedcfb"


def _select_tone(brief: str, style_tone: str | list) -> str:
    # style_tone from clarification answers takes priority (it's user-confirmed)
    if isinstance(style_tone, list):
        style_tone_str = " ".join(style_tone)
    else:
        style_tone_str = str(style_tone)

    # Score against both style_tone (weighted 3x) and brief (1x)
    text = f"{style_tone_str} {style_tone_str} {style_tone_str} {brief}".lower()
    scores = {tone: sum(kw in text for kw in kws) for tone, kws in _TONE_MAP.items()}
    best = max(scores, key=lambda t: scores[t])
    return best if scores[best] > 0 else "warm"


def _generate_via_replicate(prompt: str, duration: int) -> str | None:
    """Call Replicate MusicGen. Returns local MP3 path or None on failure."""
    try:
        import replicate
        output = replicate.run(
            _REPLICATE_MODEL,
            input={
                "prompt": prompt,
                "duration": duration,
                "model_version": "stereo-large",
                "output_format": "mp3",
                "normalization_strategy": "peak",
            },
        )
        # output is a URL string or file-like object
        audio_url = output if isinstance(output, str) else str(output)

        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        urllib.request.urlretrieve(audio_url, tmp.name)
        return tmp.name
    except Exception as e:
        console.print(f"[yellow][music_mixer] Replicate error: {e}[/yellow]")
        return None


def music_mixer(state: dict[str, Any]) -> dict[str, Any]:
    """Select tone, generate via MusicGen on Replicate, mix into branded_clip_path."""
    api_token = os.environ.get("REPLICATE_API_TOKEN", "")
    if not api_token:
        console.print("[yellow][music_mixer] No REPLICATE_API_TOKEN — skipping music.[/yellow]")
        return {"music_track_path": ""}

    branded_path = state.get("branded_clip_path", "")
    if not branded_path or not Path(branded_path).exists():
        console.print("[yellow][music_mixer] No branded clip found — skipping music.[/yellow]")
        return {"music_track_path": ""}

    brief = state.get("brief", "")
    plan = state.get("plan", {})
    # Prefer user-confirmed style_tone from clarification, fall back to plan field
    style_tone = (
        state.get("clarification_answers", {}).get("style_tone")
        or plan.get("style_tone", "")
    )
    tone = _select_tone(brief, style_tone)
    prompt = _TONE_PROMPTS[tone]

    duration_sec = int(_probe_duration(branded_path) or 30)
    duration_sec = max(5, min(duration_sec, 300))  # MusicGen cap: 5 min

    console.print(f"[cyan][music_mixer] Generating '{tone}' music ({duration_sec}s) via MusicGen…[/cyan]")
    music_path = _generate_via_replicate(prompt, duration_sec)
    if not music_path:
        console.print("[yellow][music_mixer] Generation failed — skipping music.[/yellow]")
        return {"music_track_path": ""}

    work_dir = Path(branded_path).parent
    mixed_path = str(work_dir / "branded_music.mp4")
    try:
        fc = FFmpegComposer()
        fc.mix_audio_track(
            video_path=branded_path,
            music_path=music_path,
            output_path=mixed_path,
        )
        console.print(f"[green][music_mixer] Music mixed: {mixed_path}[/green]")
        messages = state.get("messages", [])
        messages.append({
            "role": "system",
            "content": f"[music_mixer] tone={tone} mixed into {mixed_path}",
        })
        return {
            "branded_clip_path": mixed_path,
            "music_track_path": music_path,
            "messages": messages,
        }
    except Exception as e:
        console.print(f"[yellow][music_mixer] FFmpeg mix failed: {e} — skipping music.[/yellow]")
        return {"music_track_path": ""}
    finally:
        try:
            os.unlink(music_path)
        except OSError:
            pass
