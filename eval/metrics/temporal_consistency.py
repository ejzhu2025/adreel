"""temporal_consistency — ffmpeg frame extraction + PIL histogram comparison."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _extract_frame(clip_path: str, offset: float = 0.5, size: tuple[int, int] = (64, 114)) -> bytes | None:
    """Extract a single frame as raw RGB bytes via ffmpeg."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-ss", str(offset), "-i", clip_path,
                "-frames:v", "1",
                "-vf", f"scale={size[0]}:{size[1]}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ],
            capture_output=True,
            timeout=15,
        )
        raw = result.stdout
        expected = size[0] * size[1] * 3
        if len(raw) < expected:
            return None
        return raw[:expected]
    except Exception:
        return None


def _histogram_correlation(a: bytes, b: bytes) -> float:
    """
    Compute normalized dot-product correlation between two raw RGB byte sequences.
    Returns 0.0–1.0 (1.0 = identical histograms).
    """
    if len(a) != len(b) or len(a) == 0:
        return 0.0

    # Build 256-bucket histograms
    hist_a = [0] * 256
    hist_b = [0] * 256
    for pixel in a:
        hist_a[pixel] += 1
    for pixel in b:
        hist_b[pixel] += 1

    # Normalized dot product
    dot = sum(hist_a[i] * hist_b[i] for i in range(256))
    norm_a = sum(v * v for v in hist_a) ** 0.5
    norm_b = sum(v * v for v in hist_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def score(
    project_id: str,
    shot_ids: list[str],
    data_dir: str = "data",
    correlation_threshold: float = 0.3,
) -> dict[str, Any]:
    """
    Compare adjacent shot clips by histogram correlation.

    For each shot: extract 1 frame at 0.5s, then compare consecutive pairs.
    Flag pair as inconsistent if correlation < threshold.
    Score = consistent_pairs / total_pairs (1.0 if ≤1 clip).
    """
    clips_dir = Path(data_dir) / "projects" / project_id / "clips"

    frames: list[tuple[str, bytes]] = []  # (shot_id, raw_rgb)
    skipped: list[str] = []

    for shot_id in shot_ids:
        clip_path = clips_dir / f"{shot_id}.mp4"
        if not clip_path.exists():
            skipped.append(shot_id)
            continue
        raw = _extract_frame(str(clip_path))
        if raw is None:
            skipped.append(shot_id)
            continue
        frames.append((shot_id, raw))

    if len(frames) <= 1:
        return {
            "score": 1.0,
            "pairs_checked": 0,
            "inconsistent": [],
            "skipped": skipped,
        }

    inconsistent: list[dict] = []
    for i in range(len(frames) - 1):
        id_a, raw_a = frames[i]
        id_b, raw_b = frames[i + 1]
        corr = _histogram_correlation(raw_a, raw_b)
        if corr < correlation_threshold:
            inconsistent.append({"pair": [id_a, id_b], "correlation": round(corr, 3)})

    total_pairs = len(frames) - 1
    consistency_score = (total_pairs - len(inconsistent)) / total_pairs

    return {
        "score": round(consistency_score, 4),
        "pairs_checked": total_pairs,
        "inconsistent": inconsistent,
        "skipped": skipped,
    }
