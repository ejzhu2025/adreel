"""partial_executor — re-renders only affected shots; reuses existing clips for the rest."""
from __future__ import annotations

import copy
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


def partial_executor(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    affected_indices: list[int] = state.get("affected_shot_indices", [])
    shot_updates: dict[str, Any] = state.get("shot_updates", {})
    project_id = state.get("project_id", "unknown")
    quality = state.get("quality", "turbo")

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id / "clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    shot_list = plan.get("shot_list", [])
    storyboard = plan.get("storyboard", [])

    # Apply shot_updates → produce updated plan copy
    updated_plan = copy.deepcopy(plan)
    for idx_str, updates in shot_updates.items():
        i = int(idx_str)
        if 0 <= i < len(updated_plan.get("storyboard", [])):
            if "desc" in updates:
                updated_plan["storyboard"][i]["desc"] = updates["desc"]
        if 0 <= i < len(updated_plan.get("shot_list", [])):
            if "text_overlay" in updates:
                updated_plan["shot_list"][i]["text_overlay"] = updates["text_overlay"]

    # Reconstruct scene_clips list from disk (all shots)
    scene_clips: list[dict] = []
    for shot in shot_list:
        clip_path = work_dir / f"{shot['shot_id']}.mp4"
        scene_clips.append({
            "shot_id": shot["shot_id"],
            "clip_path": str(clip_path),
            "duration": float(shot.get("duration", 3.5)),
        })

    replicate_token = os.getenv("REPLICATE_API_TOKEN")
    fal_key = os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")
    rerendered_count = 0

    if (replicate_token or fal_key) and affected_indices:
        if replicate_token:
            from render.replicate_t2v import generate_clip
        else:
            from render.fal_t2v import generate_clip
        from render.ffmpeg_composer import FFmpegComposer

        fc = FFmpegComposer()
        console.print(
            f"[cyan][partial_executor] Re-rendering shots {affected_indices} "
            f"(quality={quality}), reusing {len(shot_list) - len(affected_indices)} clips[/cyan]"
        )

        def _rerender(i: int) -> tuple[int, dict]:
            shot = updated_plan["shot_list"][i]
            scene = updated_plan["storyboard"][i] if i < len(updated_plan.get("storyboard", [])) else {}
            desc = scene.get("desc", shot.get("text_overlay", "cinematic product shot"))
            prompt = (
                f"{desc}. "
                "Vertical social media video, smooth motion, vibrant colors, cinematic quality."
            )
            raw_path = str(work_dir / f"{shot['shot_id']}_raw.mp4")
            clip_path = str(work_dir / f"{shot['shot_id']}.mp4")
            duration = float(shot.get("duration", 3.5))
            generate_clip(prompt, raw_path, duration=duration, quality=quality)
            fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
            return i, {"shot_id": shot["shot_id"], "clip_path": clip_path, "duration": duration}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task_id = progress.add_task(
                f"[cyan]Re-rendering {len(affected_indices)} shot(s)…",
                total=len(affected_indices),
            )
            with ThreadPoolExecutor(max_workers=min(4, len(affected_indices))) as pool:
                futures = {pool.submit(_rerender, i): i for i in affected_indices}
                for fut in as_completed(futures):
                    i, clip = fut.result()
                    scene_clips[i] = clip
                    rerendered_count += 1
                    progress.advance(task_id)

    else:
        if not replicate_token and not fal_key:
            console.print("[yellow][partial_executor] No REPLICATE_API_TOKEN or FAL_KEY — clips unchanged[/yellow]")
        elif not affected_indices:
            console.print("[dim][partial_executor] No shots to re-render[/dim]")

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": (
            f"[partial_executor] re-rendered {rerendered_count} shots, "
            f"reused {len(shot_list) - rerendered_count} existing clips"
        ),
    })

    return {
        "scene_clips": scene_clips,
        "plan": updated_plan,
        "messages": messages,
    }
