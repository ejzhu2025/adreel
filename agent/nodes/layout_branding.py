"""layout_branding — concatenate clips, burn subtitles, add logo watermark."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from render.ffmpeg_composer import FFmpegComposer
from render.caption_renderer import CaptionRenderer


def layout_branding(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state.get("project_id", "unknown")
    scene_clips = state.get("scene_clips", [])
    caption_segments = state.get("caption_segments", [])
    brand_kit = state.get("brand_kit", {})

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id
    work_dir.mkdir(parents=True, exist_ok=True)

    fc = FFmpegComposer()
    cr = CaptionRenderer()

    # 1. Concatenate all scene clips into raw video
    clip_paths = [c["clip_path"] for c in scene_clips]
    raw_path = str(work_dir / "raw_concat.mp4")
    fc.concat_clips(clip_paths, raw_path)

    # 2. Write SRT subtitle file
    subtitle_style = brand_kit.get("subtitle_style", {})
    srt_path = str(work_dir / "captions.srt")
    cr.write_srt(caption_segments, srt_path)

    # 3. Burn subtitles onto video
    with_subs_path = str(work_dir / "with_subs.mp4")
    fc.burn_subtitles(
        input_path=raw_path,
        srt_path=srt_path,
        output_path=with_subs_path,
        subtitle_style=subtitle_style,
    )

    # 4. Add logo watermark
    logo_path = brand_kit.get("logo", {}).get("path", "assets/tong_sui_logo.png")
    safe_area = brand_kit.get("logo", {}).get("safe_area", "top_right")
    branded_path = str(work_dir / "branded.mp4")

    if Path(logo_path).exists():
        fc.add_watermark(
            input_path=with_subs_path,
            logo_path=logo_path,
            output_path=branded_path,
            position=safe_area,
        )
    else:
        # No logo available — just copy
        import shutil
        shutil.copy(with_subs_path, branded_path)

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[layout_branding] branded clip: {branded_path}",
        }
    )

    return {"branded_clip_path": branded_path, "messages": messages}
