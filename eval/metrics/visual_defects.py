"""visual_defects — ffprobe + frame sampling, reusing quality_gate helpers."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Allow importing from project root
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent.nodes.quality_gate import _probe_video, _check_blank_frame  # noqa: E402


def score(
    project_id: str,
    shot_ids: list[str],
    data_dir: str = "data",
    min_bitrate_kbps: float = 30.0,
    min_duration_sec: float = 0.3,
) -> dict[str, Any]:
    """
    Check each shot clip for visual defects.

    Defect conditions:
    - blank frame (low variance)
    - bitrate < min_bitrate_kbps kbps
    - actual duration < min_duration_sec

    Score = 1 - (defective_shots / total_shots).
    """
    clips_dir = Path(data_dir) / "projects" / project_id / "clips"
    defects: list[dict] = []
    checked = 0

    for shot_id in shot_ids:
        clip_path = clips_dir / f"{shot_id}.mp4"
        if not clip_path.exists():
            continue
        checked += 1
        clip_str = str(clip_path)
        reasons: list[str] = []

        info = _probe_video(clip_str)
        if info:
            bitrate = info.get("bit_rate")
            if bitrate is not None:
                kbps = bitrate / 1000
                if kbps < min_bitrate_kbps:
                    reasons.append(f"low bitrate {kbps:.0f}kbps")

            duration = info.get("duration")
            if duration is not None and duration < min_duration_sec:
                reasons.append(f"too short {duration:.2f}s")

        if _check_blank_frame(clip_str):
            reasons.append("blank frame detected")

        if reasons:
            defects.append({"shot_id": shot_id, "reason": ", ".join(reasons)})

    if checked == 0:
        return {"score": 1.0, "defects": [], "shots_checked": 0}

    defect_score = 1.0 - (len(defects) / checked)
    return {
        "score": round(defect_score, 4),
        "defects": defects,
        "shots_checked": checked,
    }
