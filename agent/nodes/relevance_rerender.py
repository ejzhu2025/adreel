"""relevance_rerender — re-renders low-relevance shots with enhanced T2V prompts.

Triggered by quality_gate when shots score below RELEVANCE_THRESHOLD.
Injects missing_elements from VLM feedback into the positive prompt and
forces T2V (bypasses I2V routing that caused the original mismatch).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

# Maximum number of relevance re-render retries before giving up
MAX_RELEVANCE_RETRIES = 1


def relevance_rerender(state: dict[str, Any]) -> dict[str, Any]:
    quality_result = state.get("quality_result", {})
    low_relevance_shots: list[str] = quality_result.get("low_relevance_shots", [])
    relevance_data: list[dict] = quality_result.get("relevance", [])

    scene_clips: list[dict] = list(state.get("scene_clips", []))
    plan = state.get("plan", {})
    project_id = state.get("project_id", "unknown")
    quality = state.get("quality", "turbo")
    attempt = state.get("relevance_rerender_attempt", 0)
    t2v_prompts = state.get("t2v_prompts", {})

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id / "clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Lookup tables
    relevance_by_shot = {r["shot_id"]: r for r in relevance_data}
    clip_index = {c["shot_id"]: i for i, c in enumerate(scene_clips)}

    shot_list = plan.get("shot_list", [])
    storyboard = plan.get("storyboard", [])
    scene_by_shot: dict[str, dict] = {
        shot["shot_id"]: {"shot": shot, "scene": storyboard[i] if i < len(storyboard) else {}, "index": i}
        for i, shot in enumerate(shot_list)
    }

    product_image_path = state.get("product_image_path", "")
    outro_shot_id = shot_list[-1]["shot_id"] if shot_list else None

    # Never T2V-rerender the outro when a product image is uploaded —
    # the FLUX Kontext + I2V result already shows the real product.
    # Re-rendering with T2V would replace it with a generic AI scene.
    skipped_outro: list[str] = []
    if outro_shot_id and product_image_path and Path(product_image_path).exists():
        if outro_shot_id in low_relevance_shots:
            skipped_outro.append(outro_shot_id)
            low_relevance_shots = [s for s in low_relevance_shots if s != outro_shot_id]
            console.print(
                f"[dim][relevance_rerender] Skipping outro {outro_shot_id} — "
                "product image outro is exempt from T2V re-render[/dim]"
            )

    rerendered: list[str] = []
    fal_key = os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")

    if not fal_key:
        console.print("[yellow][relevance_rerender] No FAL_KEY — skipping re-render[/yellow]")
    elif not low_relevance_shots:
        console.print("[dim][relevance_rerender] No low-relevance shots to re-render[/dim]")
    else:
        from render.fal_t2v import generate_clip
        from render.ffmpeg_composer import FFmpegComposer

        fc = FFmpegComposer()
        console.print(
            f"[cyan][relevance_rerender] Attempt {attempt + 1}/{MAX_RELEVANCE_RETRIES} — "
            f"re-rendering: {low_relevance_shots}[/cyan]"
        )

        def _build_enhanced_prompt(shot_id: str) -> tuple[str, str]:
            """Inject missing_elements at the front of the positive prompt."""
            rel = relevance_by_shot.get(shot_id, {})
            missing = rel.get("missing_elements", [])
            reason = rel.get("reason", "")
            info = scene_by_shot.get(shot_id, {})
            scene = info.get("scene", {})
            base_desc = scene.get("desc", "cinematic product shot")

            compiled = t2v_prompts.get(shot_id, {})
            if isinstance(compiled, dict):
                positive = compiled.get("positive", "") or base_desc
                negative = compiled.get("negative", "")
            else:
                positive = str(compiled) if compiled else base_desc
                negative = ""

            # Prepend missing elements as MUST INCLUDE directive
            if missing:
                missing_str = ", ".join(missing)
                positive = f"MUST INCLUDE: {missing_str}. {positive}"

            console.print(
                f"  [dim]{shot_id} enhanced prompt: {positive[:100]}…[/dim]"
            )
            return positive, negative

        def _rerender_shot(shot_id: str) -> tuple[str, dict | None]:
            info = scene_by_shot.get(shot_id)
            if not info:
                return shot_id, None
            shot = info["shot"]
            duration = float(shot.get("duration", 3.5))
            positive, negative = _build_enhanced_prompt(shot_id)

            raw_path = str(work_dir / f"{shot_id}_rel_raw.mp4")
            clip_path = str(work_dir / f"{shot_id}.mp4")

            try:
                generate_clip(positive, raw_path, duration=duration, quality=quality,
                              negative_prompt=negative)
                fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
                return shot_id, {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
            except Exception as e:
                console.print(f"[red][relevance_rerender] {shot_id} failed: {e}[/red]")
                return shot_id, None

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task_id = progress.add_task(
                f"[cyan]Re-rendering {len(low_relevance_shots)} low-relevance shot(s)…",
                total=len(low_relevance_shots),
            )
            with ThreadPoolExecutor(max_workers=min(4, len(low_relevance_shots))) as pool:
                futures = {pool.submit(_rerender_shot, sid): sid for sid in low_relevance_shots}
                for fut in as_completed(futures):
                    shot_id, clip = fut.result()
                    if clip and shot_id in clip_index:
                        scene_clips[clip_index[shot_id]] = clip
                        rerendered.append(shot_id)
                    progress.advance(task_id)

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": (
            f"[relevance_rerender] attempt={attempt + 1} "
            f"re-rendered={rerendered} of requested={low_relevance_shots}"
        ),
    })

    return {
        "scene_clips": scene_clips,
        "relevance_rerender_attempt": attempt + 1,
        "messages": messages,
    }
