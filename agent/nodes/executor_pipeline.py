"""executor_pipeline — render each shot as a clip using fal.ai T2V or PIL fallback."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from render.frame_generator import FrameGenerator
from render.ffmpeg_composer import FFmpegComposer


def executor_pipeline(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    brand_kit = state.get("brand_kit", {})
    project_id = state.get("project_id", "unknown")

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id / "clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    fc = FFmpegComposer()
    shot_list = plan.get("shot_list", [])
    scene_clips: list[dict] = []

    replicate_token = os.getenv("REPLICATE_API_TOKEN")
    fal_key = os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")
    import sys
    # fal.ai takes priority when both keys are set — better concurrency, no burst limits
    _provider = "fal" if fal_key else ("replicate" if replicate_token else "pil")
    print(f"[executor] provider={_provider}", file=sys.stderr, flush=True)

    if fal_key or replicate_token:
        # ── AI T2V path (fal.ai preferred, Replicate fallback) ───────────
        if fal_key:
            os.environ.setdefault("FAL_KEY", fal_key)
            from render.fal_t2v import generate_clip
            _using_replicate = False
        else:
            from render.replicate_t2v import generate_clip
            from render.replicate_i2v import (
                generate_clip_from_image, build_shot_motion_prompt, build_outro_motion_prompt,
            )
            _using_replicate = True

        storyboard = plan.get("storyboard", [])
        # Build shot_id-based lookup, fall back to index
        storyboard_by_shot_id = {s.get("shot_id"): s for s in storyboard if s.get("shot_id")}
        style_tone = state.get("clarification_answers", {}).get("style_tone", ["fresh"])

        quality = state.get("quality", "turbo")

        from render.shot_renderer import render_shot

        def _process_shot(args: tuple[int, dict]) -> dict:
            i, shot = args
            return render_shot(
                i=i,
                shot=shot,
                total_shots=len(shot_list),
                work_dir=work_dir,
                fc=fc,
                generate_clip=generate_clip,
                using_replicate=_using_replicate,
                state=state,
                storyboard_by_shot_id=storyboard_by_shot_id,
            )

        results: dict[int, dict] = {}
        total_shots = len(shot_list)
        done_shots = 0
        # Replicate: try up to 3 concurrent; will retry on 429 automatically.
        # fal.ai supports higher concurrency.
        _max_workers = min(3, len(shot_list)) if _using_replicate else min(4, len(shot_list))
        with ThreadPoolExecutor(max_workers=_max_workers) as pool:
            futures = {pool.submit(_process_shot, (i, s)): i for i, s in enumerate(shot_list)}
            for fut in as_completed(futures):
                idx = futures[fut]
                results[idx] = fut.result()
                done_shots += 1
                import agent.deps as _deps
                _deps.emit({"type": "shot_progress", "done": done_shots, "total": total_shots})
        scene_clips = [results[i] for i in range(len(shot_list))]

    else:
        # ── PIL fallback (no REPLICATE_API_TOKEN and no FAL_KEY) ──────────
        fg = FrameGenerator(brand_kit=brand_kit, work_dir=work_dir)
        product_image_path = state.get("product_image_path", "")
        logo_path = brand_kit.get("logo", {}).get("path", "")

        for i, shot in enumerate(shot_list):
            shot_id = shot["shot_id"]
            duration = float(shot.get("duration", 2.5))
            shot_type = shot.get("type", "wide")
            is_outro = (i == len(shot_list) - 1)

            # Use product image as background for outro if available
            bg_path = (
                product_image_path
                if is_outro and product_image_path and Path(product_image_path).exists()
                else ""
            )

            frame_path = fg.generate_frame(
                shot_id=shot_id,
                shot_type=shot_type,
                text_overlay="",
                scene_index=i,
                is_intro=(i == 0),
                is_outro=is_outro,
                background_image_path=bg_path,
                logo_path=logo_path if is_outro else "",
            )

            clip_path = work_dir / f"{shot_id}.mp4"
            fc.image_to_clip(
                image_path=str(frame_path),
                output_path=str(clip_path),
                duration=duration,
                width=1080,
                height=1920,
                ken_burns=(shot_type not in ("transition",) and not is_outro),
            )

            scene_clips.append(
                {"shot_id": shot_id, "clip_path": str(clip_path), "duration": duration}
            )
            import agent.deps as _deps
            _deps.emit({"type": "shot_progress", "done": len(scene_clips), "total": len(shot_list)})

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[executor] {len(scene_clips)} clips rendered to {work_dir}",
        }
    )

    return {"scene_clips": scene_clips, "messages": messages}
