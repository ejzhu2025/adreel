"""executor_pipeline — render each shot as a clip using fal.ai T2V or PIL fallback."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from render.frame_generator import FrameGenerator
from render.ffmpeg_composer import FFmpegComposer

console = Console()


def executor_pipeline(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    brand_kit = state.get("brand_kit", {})
    project_id = state.get("project_id", "unknown")

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id / "clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    fc = FFmpegComposer()
    shot_list = plan.get("shot_list", [])
    scene_clips: list[dict] = []

    fal_key = os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")
    import sys
    print(f"[executor] FAL_KEY present={bool(fal_key)} val_prefix={str(fal_key)[:12] if fal_key else 'NONE'}", file=sys.stderr, flush=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Rendering shots…", total=len(shot_list))

        if fal_key:
            # ── fal.ai T2V path ──────────────────────────────────────────────
            os.environ.setdefault("FAL_KEY", fal_key)
            from render.fal_t2v import generate_clip

            storyboard = plan.get("storyboard", [])
            style_tone = state.get("clarification_answers", {}).get("style_tone", ["fresh"])

            def _process_shot(args: tuple[int, dict]) -> dict:
                i, shot = args
                scene = storyboard[i] if i < len(storyboard) else {}
                desc = scene.get("desc", shot.get("text_overlay", "cinematic product shot"))
                tone_str = ", ".join(style_tone) if isinstance(style_tone, list) else str(style_tone)
                prompt = (
                    f"{desc}. Style: {tone_str}. "
                    "Vertical social media video, smooth motion, vibrant colors, cinematic quality."
                )
                raw_path = str(work_dir / f"{shot['shot_id']}_raw.mp4")
                clip_path = str(work_dir / f"{shot['shot_id']}.mp4")
                duration = float(shot.get("duration", 3.5))
                generate_clip(prompt, raw_path, duration=duration)
                fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
                return {"shot_id": shot["shot_id"], "clip_path": clip_path, "duration": duration}

            results: dict[int, dict] = {}
            with ThreadPoolExecutor(max_workers=min(6, len(shot_list))) as pool:
                futures = {pool.submit(_process_shot, (i, s)): i for i, s in enumerate(shot_list)}
                for fut in as_completed(futures):
                    idx = futures[fut]
                    results[idx] = fut.result()
                    progress.advance(task)
            scene_clips = [results[i] for i in range(len(shot_list))]

        else:
            # ── PIL fallback (existing behavior) ─────────────────────────────
            fg = FrameGenerator(brand_kit=brand_kit, work_dir=work_dir)

            for i, shot in enumerate(shot_list):
                shot_id = shot["shot_id"]
                duration = float(shot.get("duration", 2.5))
                text_overlay = shot.get("text_overlay", "")
                shot_type = shot.get("type", "wide")

                frame_path = fg.generate_frame(
                    shot_id=shot_id,
                    shot_type=shot_type,
                    text_overlay=text_overlay,
                    scene_index=i,
                    is_intro=(i == 0),
                    is_outro=(i == len(shot_list) - 1),
                )

                clip_path = work_dir / f"{shot_id}.mp4"
                fc.image_to_clip(
                    image_path=str(frame_path),
                    output_path=str(clip_path),
                    duration=duration,
                    width=1080,
                    height=1920,
                    ken_burns=(shot_type not in ("text", "transition")),
                )

                scene_clips.append(
                    {"shot_id": shot_id, "clip_path": str(clip_path), "duration": duration}
                )
                progress.advance(task)

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[executor] {len(scene_clips)} clips rendered to {work_dir}",
        }
    )

    return {"scene_clips": scene_clips, "messages": messages}
