"""cost_latency — timing and output size tracker. No score, no API."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def measure(
    plan_sec: float,
    execute_sec: float,
    quality_result: dict | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """
    Collect timing and output size metrics.

    Args:
        plan_sec: seconds spent on planning phase
        execute_sec: seconds spent on execution phase
        quality_result: quality_gate result dict (for qc_attempts)
        output_path: path to final output video file

    Returns dict with total_sec, plan_sec, execute_sec, qc_attempts, output_size_mb.
    """
    total_sec = plan_sec + execute_sec

    qc_attempts = 0
    if isinstance(quality_result, dict):
        qc_attempts = quality_result.get("attempt", 1)

    output_size_mb = None
    if output_path:
        p = Path(output_path)
        if p.exists():
            output_size_mb = round(p.stat().st_size / (1024 * 1024), 2)

    return {
        "total_sec": round(total_sec, 2),
        "plan_sec": round(plan_sec, 2),
        "execute_sec": round(execute_sec, 2),
        "qc_attempts": qc_attempts,
        "output_size_mb": output_size_mb,
    }
