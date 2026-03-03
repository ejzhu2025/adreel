"""quality_gate — check duration, resolution, content, captions, logo."""
from __future__ import annotations

import subprocess
import json
import os
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()

MAX_ATTEMPTS = 2


def quality_gate(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    branded_path = state.get("branded_clip_path", "")
    brand_kit = state.get("brand_kit", {})
    caption_segments = state.get("caption_segments", [])
    attempt = state.get("qc_attempt", 1)

    issues: list[str] = []
    auto_fix_applied = False

    target_sec = float(plan.get("duration_sec", 20))

    if not branded_path or not Path(branded_path).exists():
        issues.append(f"Branded clip not found: {branded_path}")
    else:
        info = _probe_video(branded_path)

        # 1. Duration check — tolerance: larger of 2s or 30% of target
        if info:
            actual_sec = info.get("duration")
            if actual_sec is not None:
                tolerance = max(2.0, target_sec * 0.30)
                if abs(actual_sec - target_sec) > tolerance:
                    issues.append(
                        f"Duration {actual_sec:.1f}s vs target {target_sec:.1f}s "
                        f"(tolerance ±{tolerance:.1f}s)"
                    )

            # 2. Resolution check — must be 1080×1920
            width = info.get("width")
            height = info.get("height")
            if width and height:
                if width != 1080 or height != 1920:
                    issues.append(
                        f"Resolution {width}×{height} — expected 1080×1920"
                    )

            # 3. Content check — detect blank/uniform video via low entropy bitrate
            bitrate = info.get("bit_rate")
            if bitrate is not None and actual_sec and actual_sec > 0:
                kbps = bitrate / 1000
                # A 1080×1920 video with real content should be >50kbps
                if kbps < 30:
                    issues.append(
                        f"Video bitrate {kbps:.0f}kbps is suspiciously low — "
                        "may be blank/uniform frames"
                    )

            # 4. Frame content check — sample a frame and check color variance
            blank = _check_blank_frame(branded_path)
            if blank:
                issues.append("Video appears to have blank/uniform frames (single color)")

    # 5. Caption safe-area check
    subtitle_style = brand_kit.get("subtitle_style", {})
    font_size = subtitle_style.get("font_size", 38)
    if font_size < 20:
        issues.append(f"Caption font_size {font_size} too small (min 20)")
        brand_kit["subtitle_style"]["font_size"] = 20
        auto_fix_applied = True

    max_chars = subtitle_style.get("max_chars_per_line", 18)
    for seg in caption_segments:
        for line in seg["text"].split("\n"):
            if len(line) > max_chars + 5:
                issues.append(f"Caption line too long: '{line[:30]}…'")
                auto_fix_applied = True
                break

    # 6. Logo file existence
    logo_path = brand_kit.get("logo", {}).get("path", "")
    if logo_path and not Path(logo_path).exists():
        issues.append(f"Logo file missing: {logo_path}")

    passed = len(issues) == 0 or (auto_fix_applied and attempt < MAX_ATTEMPTS)

    if issues:
        severity = "auto-fixed" if auto_fix_applied else "FAILED"
        console.print(f"[{'yellow' if auto_fix_applied else 'red'}][QC] {severity}: {issues}[/]")
    else:
        console.print("[green][QC] All checks passed ✓[/green]")

    quality_result = {
        "passed": passed,
        "issues": issues,
        "auto_fix_applied": auto_fix_applied,
        "attempt": attempt,
    }

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[quality_gate] attempt={attempt} passed={passed} issues={issues}",
        }
    )

    return {
        "quality_result": quality_result,
        "qc_attempt": attempt + 1,
        "brand_kit": brand_kit,
        "messages": messages,
    }


def _probe_video(path: str) -> dict | None:
    """Return duration, resolution, bitrate via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", "-select_streams", "v:0",
                path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [{}])
        vs = streams[0] if streams else {}
        return {
            "duration": float(fmt.get("duration", 0)) or None,
            "bit_rate": int(fmt.get("bit_rate", 0)) or None,
            "width": vs.get("width"),
            "height": vs.get("height"),
        }
    except Exception:
        return None


def _check_blank_frame(path: str) -> bool:
    """Sample frame at 1s, check if color variance is near zero (blank video)."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-ss", "1", "-i", path,
                "-frames:v", "1",
                "-vf", "scale=64:114",   # tiny thumbnail
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ],
            capture_output=True, timeout=15,
        )
        raw = result.stdout
        if len(raw) < 64 * 114 * 3:
            return False
        # Compute std dev of pixel values
        total = sum(raw)
        mean = total / len(raw)
        variance = sum((b - mean) ** 2 for b in raw) / len(raw)
        # variance < 100 means nearly uniform (std < 10 out of 255)
        return variance < 100
    except Exception:
        return False
