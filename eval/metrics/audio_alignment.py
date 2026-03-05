"""audio_alignment — SRT parsing and validation. No external API."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _parse_srt(srt_text: str) -> list[dict]:
    """Parse SRT content into list of {index, start_sec, end_sec, text}."""
    segments = []
    blocks = re.split(r"\n\n+", srt_text.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        # Line 0: index, Line 1: timecode, Lines 2+: text
        timecode_line = lines[1]
        text = " ".join(lines[2:]).strip()
        match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})",
            timecode_line,
        )
        if not match:
            continue
        start_sec = _tc_to_sec(match.group(1))
        end_sec = _tc_to_sec(match.group(2))
        segments.append({"start_sec": start_sec, "end_sec": end_sec, "text": text})
    return segments


def _tc_to_sec(tc: str) -> float:
    """Convert HH:MM:SS,mmm or HH:MM:SS.mmm to seconds."""
    tc = tc.replace(",", ".")
    parts = tc.split(":")
    h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def score(
    project_id: str,
    data_dir: str = "data",
    video_duration_sec: float | None = None,
    min_seg_duration: float = 0.3,
    max_seg_duration: float = 8.0,
    coverage_tolerance: float = 0.15,
) -> dict[str, Any]:
    """
    Validate SRT captions for the project.

    Checks:
    - Each segment duration within [min_seg_duration, max_seg_duration]
    - No overlap with previous segment
    - Text non-empty
    - Total SRT coverage vs video duration within ±coverage_tolerance (if known)

    Score = valid_segments / total_segments.
    """
    srt_path = Path(data_dir) / "projects" / project_id / "captions.srt"
    if not srt_path.exists():
        return {"score": None, "issues": [], "total_segments": 0, "skipped": True}

    try:
        srt_text = srt_path.read_text(encoding="utf-8")
    except Exception as e:
        return {"score": None, "issues": [str(e)], "total_segments": 0, "skipped": True}

    segments = _parse_srt(srt_text)
    if not segments:
        return {"score": 0.0, "issues": ["No valid SRT segments found"], "total_segments": 0, "skipped": False}

    issues: list[str] = []
    valid = 0
    prev_end = -1.0

    for i, seg in enumerate(segments):
        seg_issues: list[str] = []
        duration = seg["end_sec"] - seg["start_sec"]

        if duration < min_seg_duration:
            seg_issues.append(f"seg {i+1}: too short ({duration:.2f}s)")
        elif duration > max_seg_duration:
            seg_issues.append(f"seg {i+1}: too long ({duration:.2f}s)")

        if seg["start_sec"] < prev_end:
            seg_issues.append(f"seg {i+1}: overlaps previous (starts {seg['start_sec']:.2f}s < prev_end {prev_end:.2f}s)")

        if not seg["text"].strip():
            seg_issues.append(f"seg {i+1}: empty text")

        if seg_issues:
            issues.extend(seg_issues)
        else:
            valid += 1

        prev_end = seg["end_sec"]

    # Coverage check
    if video_duration_sec and video_duration_sec > 0:
        srt_end = segments[-1]["end_sec"] if segments else 0.0
        ratio = srt_end / video_duration_sec
        if abs(ratio - 1.0) > coverage_tolerance:
            issues.append(
                f"SRT coverage {srt_end:.1f}s vs video {video_duration_sec:.1f}s "
                f"(ratio {ratio:.2f}, tolerance ±{coverage_tolerance:.0%})"
            )

    adherence_score = valid / len(segments) if segments else 0.0
    return {
        "score": round(adherence_score, 4),
        "issues": issues,
        "total_segments": len(segments),
        "valid_segments": valid,
        "skipped": False,
    }
