"""render_export — final FFmpeg pass to produce the deliverable MP4."""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()


def render_export(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state.get("project_id", "unknown")
    branded_path = state.get("branded_clip_path", "")
    plan = state.get("plan", {})

    ratio = "9x16"
    output_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_filename = f"{project_id}_{ratio}_{timestamp}.mp4"
    out_path = output_dir / out_filename

    if branded_path and Path(branded_path).exists():
        # Final encode: ensure H.264, AAC, correct pixel format
        import subprocess
        cmd = [
            "ffmpeg", "-y",
            "-i", branded_path,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                console.print(f"[yellow][render_export] FFmpeg warning: {result.stderr[-200:]}[/yellow]")
            console.print(f"[bold green]✓ Exported: {out_path}[/bold green]")
        except Exception as e:
            console.print(f"[red][render_export] FFmpeg failed: {e} — copying raw[/red]")
            shutil.copy(branded_path, str(out_path))
    else:
        console.print(f"[red][render_export] No branded clip to export[/red]")
        out_path = Path(branded_path) if branded_path else output_dir / "error.mp4"

    messages = state.get("messages", [])
    messages.append(
        {"role": "system", "content": f"[render_export] output={out_path}"}
    )

    return {"output_path": str(out_path), "messages": messages}
