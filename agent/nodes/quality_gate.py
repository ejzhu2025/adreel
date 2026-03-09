"""quality_gate — check duration, resolution, content, captions, logo, shot relevance."""
from __future__ import annotations

import base64
import subprocess
import json
import os
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()

MAX_ATTEMPTS = 2
# Relevance score threshold (0-10). Shots below this are flagged.
RELEVANCE_THRESHOLD = 5


def _get_config(key: str, default):
    """Read a runtime config value from system_config table, fall back to default."""
    try:
        import json as _json
        import agent.deps as _deps
        val = _deps.db().get_system_config(key)
        return _json.loads(val) if val is not None else default
    except Exception:
        return default


def quality_gate(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    branded_path = state.get("branded_clip_path", "")
    brand_kit = state.get("brand_kit", {})
    caption_segments = state.get("caption_segments", [])
    attempt = state.get("qc_attempt", 1)
    max_attempts = _get_config("max_qc_attempts", MAX_ATTEMPTS)
    relevance_threshold = _get_config("relevance_threshold", RELEVANCE_THRESHOLD)

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

    # 6. Logo file existence + integrity
    logo_path = brand_kit.get("logo", {}).get("path", "")
    if logo_path and not Path(logo_path).exists():
        issues.append(f"Logo file missing: {logo_path}")
    elif logo_path and Path(logo_path).exists():
        size = Path(logo_path).stat().st_size
        if size < 67:  # 67B is the absolute minimum valid PNG size
            issues.append(f"Logo file too small ({size}B) — may be corrupt")
        else:
            try:
                from PIL import Image
                with Image.open(logo_path) as img:
                    w, h = img.size
                    if w < 20 or h < 20:
                        issues.append(f"Logo too small ({w}×{h}px) — min 20×20")
            except Exception as exc:
                issues.append(f"Logo file unreadable: {exc}")

    # 7. Shot relevance — VLM checks each generated clip against its storyboard desc
    scene_clips = state.get("scene_clips", [])
    plan_storyboard = plan.get("storyboard", [])
    relevance_results: list[dict] = []
    low_relevance_shots: list[str] = []

    if scene_clips and plan_storyboard and os.getenv("ANTHROPIC_API_KEY"):
        relevance_results = _check_shot_relevance(scene_clips, plan_storyboard)
        for r in relevance_results:
            if r["score"] < relevance_threshold:
                low_relevance_shots.append(r["shot_id"])
                issues.append(
                    f"Shot {r['shot_id']} low relevance score {r['score']}/10: {r['reason']}"
                )
        if relevance_results:
            scores = [r["score"] for r in relevance_results]
            avg = sum(scores) / len(scores)
            console.print(
                f"[cyan][QC][/cyan] Relevance: avg {avg:.1f}/10 "
                f"| low: {low_relevance_shots or 'none'}"
            )
    else:
        console.print("[dim][QC] Relevance check skipped (no clips/storyboard/API key)[/dim]")

    passed = len(issues) == 0 or (auto_fix_applied and attempt < max_attempts)

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
        "relevance": relevance_results,
        "low_relevance_shots": low_relevance_shots,
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


def _extract_keyframe_b64(clip_path: str) -> str | None:
    """Extract a single keyframe from a clip at 50% duration, return as base64 JPEG."""
    try:
        # Probe duration first
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", clip_path],
            capture_output=True, text=True, timeout=10,
        )
        fmt = json.loads(probe.stdout).get("format", {})
        duration = float(fmt.get("duration", 1.0))
        seek = max(0.1, duration * 0.5)

        result = subprocess.run(
            [
                "ffmpeg", "-ss", str(seek), "-i", clip_path,
                "-frames:v", "1",
                "-vf", "scale=540:960",  # half-res thumbnail
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        return base64.standard_b64encode(result.stdout).decode("utf-8")
    except Exception:
        return None


def _check_shot_relevance(
    scene_clips: list[dict],
    storyboard: list[dict],
) -> list[dict]:
    """Score each shot's generated clip against its storyboard desc using Claude Vision.

    Returns list of {shot_id, score (0-10), reason, missing_elements}.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()
    except Exception as e:
        console.print(f"[dim][QC] Relevance check skipped: {e}[/dim]")
        return []

    scenes_by_idx = {i: s for i, s in enumerate(storyboard)}
    results: list[dict] = []

    for i, clip_info in enumerate(scene_clips):
        shot_id = clip_info.get("shot_id", f"S{i+1}")
        clip_path = clip_info.get("clip_path", "")
        scene = scenes_by_idx.get(i, {})
        desc = scene.get("desc", "")

        if not desc or not clip_path or not Path(clip_path).exists():
            continue

        img_b64 = _extract_keyframe_b64(clip_path)
        if not img_b64:
            console.print(f"[dim][QC] Skipping {shot_id} — keyframe extraction failed[/dim]")
            continue

        prompt = (
            f"Storyboard description for this shot:\n\"{desc}\"\n\n"
            "Look at the video frame above and score how well it matches the storyboard description.\n"
            "Focus on: subject matter, environment/background, lighting, composition, colors.\n\n"
            "Respond with ONLY a JSON object (no markdown):\n"
            "{\n"
            '  "score": <integer 0-10>,\n'
            '  "reason": "<one sentence explaining the score>",\n'
            '  "missing_elements": ["<element1>", "<element2>"]\n'
            "}\n\n"
            "Score guide: 0=completely wrong, 5=partially matches, 10=perfect match."
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",  # cheap + fast for VLM
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": img_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw = response.content[0].text.strip()
            # Strip markdown fences if present
            raw = raw.strip("```json").strip("```").strip()
            data = json.loads(raw)
            score = int(data.get("score", 0))
            reason = data.get("reason", "")
            missing = data.get("missing_elements", [])
            results.append({
                "shot_id": shot_id,
                "score": score,
                "reason": reason,
                "missing_elements": missing,
            })
            icon = "✅" if score >= relevance_threshold else "⚠️"
            console.print(
                f"  {icon} {shot_id} [{score}/10] {reason[:80]}"
            )
        except Exception as e:
            console.print(f"[dim][QC] {shot_id} relevance check error: {e}[/dim]")

    return results


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
