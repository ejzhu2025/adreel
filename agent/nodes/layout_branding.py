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

    # 2. Write SRT for reference only — subtitles are NOT burned into the video.
    #    Text overlays in the shot list are handled by FrameGenerator on the outro frame.
    srt_path = str(work_dir / "captions.srt")
    cr.write_srt(caption_segments, srt_path)

    # 3. No subtitle burn, no full-video watermark — copy raw concat directly.
    branded_path = str(work_dir / "branded.mp4")
    import shutil
    shutil.copy(raw_path, branded_path)

    # Keep with_subs.mp4 as an alias so existing file references don't break.
    shutil.copy(raw_path, str(work_dir / "with_subs.mp4"))

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[layout_branding] branded clip: {branded_path}",
        }
    )

    return {"branded_clip_path": branded_path, "messages": messages}
