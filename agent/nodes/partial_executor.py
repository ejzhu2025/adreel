"""partial_executor — re-renders affected shots, adds new shots, or removes shots."""
from __future__ import annotations

import copy
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()


def partial_executor(state: dict[str, Any]) -> dict[str, Any]:
    change_type = state.get("change_type", "local")

    if change_type == "add_scene":
        return _add_scenes(state)
    if change_type == "remove_scene":
        return _remove_scenes(state)
    # "local" (default)
    return _rerender_shots(state)


# ── Local: re-render existing shots ──────────────────────────────────────────

def _rerender_shots(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    affected_indices: list[int] = state.get("affected_shot_indices", [])
    shot_updates: dict[str, Any] = state.get("shot_updates", {})
    project_id = state.get("project_id", "unknown")
    quality = state.get("quality", "turbo")

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id / "clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    shot_list = plan.get("shot_list", [])

    # Apply shot_updates → produce updated plan copy
    updated_plan = copy.deepcopy(plan)
    for idx_str, updates in shot_updates.items():
        i = int(idx_str)
        if 0 <= i < len(updated_plan.get("storyboard", [])):
            if "desc" in updates:
                updated_plan["storyboard"][i]["desc"] = updates["desc"]

    # Reconstruct scene_clips list from disk (all shots)
    scene_clips: list[dict] = []
    for shot in shot_list:
        clip_path = work_dir / f"{shot['shot_id']}.mp4"
        scene_clips.append({
            "shot_id": shot["shot_id"],
            "clip_path": str(clip_path),
            "duration": float(shot.get("duration", 3.5)),
        })

    # Guard: drop indices that exceed the current shot_list length
    shot_count = len(updated_plan.get("shot_list", []))
    affected_indices = [i for i in affected_indices if i < shot_count]

    replicate_token = os.getenv("REPLICATE_API_TOKEN")
    fal_key = os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")
    rerendered_count = 0

    if (replicate_token or fal_key) and affected_indices:
        generate_clip = _get_t2v_fn(fal_key, replicate_token)
        from render.ffmpeg_composer import FFmpegComposer
        fc = FFmpegComposer()

        total_shots = len(updated_plan.get("shot_list", []))
        storyboard = updated_plan.get("storyboard", [])
        storyboard_by_shot_id = {s.get("shot_id"): s for s in storyboard if s.get("shot_id")}
        using_replicate = not fal_key

        from render.shot_renderer import render_shot
        # Merge updated_plan into a local state copy so render_shot sees the latest plan
        _render_state = {**state, "plan": updated_plan}

        def _rerender(i: int) -> tuple[int, dict]:
            shot = updated_plan["shot_list"][i]
            clip = render_shot(
                i=i,
                shot=shot,
                total_shots=total_shots,
                work_dir=work_dir,
                fc=fc,
                generate_clip=generate_clip,
                using_replicate=using_replicate,
                state=_render_state,
                storyboard_by_shot_id=storyboard_by_shot_id,
            )
            return i, clip

        with ThreadPoolExecutor(max_workers=min(4, len(affected_indices))) as pool:
            futures = {pool.submit(_rerender, i): i for i in affected_indices}
            for fut in as_completed(futures):
                i, clip = fut.result()
                scene_clips[i] = clip
                rerendered_count += 1
                import agent.deps as _deps
                _deps.emit({"type": "shot_progress", "done": rerendered_count, "total": len(affected_indices)})
    else:
        if not replicate_token and not fal_key:
            console.print("[yellow][partial_executor] No API token — clips unchanged[/yellow]")

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": f"[partial_executor] re-rendered {rerendered_count} shots, reused {len(shot_list) - rerendered_count} clips",
    })
    return {"scene_clips": scene_clips, "plan": updated_plan, "messages": messages}


# ── Add scene: generate new shot(s) and insert into plan ─────────────────────

def _add_scenes(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    new_shots: list[dict] = state.get("new_shots", [])
    project_id = state.get("project_id", "unknown")
    quality = state.get("quality", "turbo")

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id / "clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    updated_plan = copy.deepcopy(plan)
    existing_shot_list = updated_plan.get("shot_list", [])
    existing_storyboard = updated_plan.get("storyboard", [])

    # Build scene_clips from existing clips on disk
    scene_clips: list[dict] = []
    for shot in existing_shot_list:
        clip_path = work_dir / f"{shot['shot_id']}.mp4"
        scene_clips.append({
            "shot_id": shot["shot_id"],
            "clip_path": str(clip_path),
            "duration": float(shot.get("duration", 3.5)),
        })

    replicate_token = os.getenv("REPLICATE_API_TOKEN")
    fal_key = os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")
    generated_count = 0

    for new_shot_spec in new_shots:
        position = new_shot_spec.get("position", "last")
        desc = new_shot_spec.get("desc", "cinematic product shot, smooth motion")
        shot_type = new_shot_spec.get("type", "lifestyle")
        duration = float(new_shot_spec.get("duration", 2.0))

        # Assign a new shot_id (next in sequence)
        next_idx = len(existing_shot_list) + 1
        shot_id = f"S{next_idx}"

        # Determine insertion index
        if position == "first":
            insert_at = 0
        elif position == "last" or position is None:
            insert_at = len(existing_shot_list)
        elif position.startswith("after:"):
            try:
                after_idx = int(position.split(":")[1])
                insert_at = after_idx + 1
            except (ValueError, IndexError):
                insert_at = len(existing_shot_list)
        else:
            insert_at = len(existing_shot_list)

        clip_path = str(work_dir / f"{shot_id}.mp4")

        if replicate_token or fal_key:
            generate_clip = _get_t2v_fn(fal_key, replicate_token)
            from render.ffmpeg_composer import FFmpegComposer
            fc = FFmpegComposer()
            style_tone = state.get("clarification_answers", {}).get("style_tone", ["cinematic"])
            tone_str = ", ".join(style_tone) if isinstance(style_tone, list) else str(style_tone)
            prompt = (
                f"{desc}. Style: {tone_str}. "
                "Vertical 9:16, smooth motion, vibrant colors, cinematic quality. "
                "No text overlays, no captions, no watermarks."
            )
            raw_path = str(work_dir / f"{shot_id}_raw.mp4")
            try:
                generate_clip(prompt, raw_path, duration=duration, quality=quality)
                fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
                generated_count += 1
                print(f"[partial_executor] Added shot {shot_id} ✓", file=sys.stderr)
            except Exception as e:
                print(f"[partial_executor] Failed to generate new shot {shot_id}: {e}", file=sys.stderr)
                clip_path = ""  # skip if generation failed

        # Insert into plan and scene_clips
        new_storyboard_entry = {
            "scene": insert_at + 1,
            "desc": desc,
            "duration": duration,
            "asset_hint": shot_type,
        }
        new_shot_entry = {
            "shot_id": shot_id,
            "type": shot_type,
            "asset": "generate",
            "duration": duration,
        }
        new_clip_entry = {
            "shot_id": shot_id,
            "clip_path": clip_path,
            "duration": duration,
        }

        existing_storyboard.insert(insert_at, new_storyboard_entry)
        existing_shot_list.insert(insert_at, new_shot_entry)
        scene_clips.insert(insert_at, new_clip_entry)

    # Renumber storyboard scenes and shot_ids
    for i, (scene, shot, clip) in enumerate(zip(existing_storyboard, existing_shot_list, scene_clips)):
        scene["scene"] = i + 1
        shot["shot_id"] = f"S{i+1}"
        clip["shot_id"] = f"S{i+1}"
        # Rename clip file on disk if it exists and name differs
        old_path = Path(clip["clip_path"])
        new_path = work_dir / f"S{i+1}.mp4"
        if old_path.exists() and old_path != new_path:
            old_path.rename(new_path)
        clip["clip_path"] = str(new_path)

    updated_plan["storyboard"] = existing_storyboard
    updated_plan["shot_list"] = existing_shot_list
    updated_plan["duration_sec"] = round(sum(s.get("duration", 2.0) for s in existing_shot_list), 1)

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": f"[partial_executor] added {generated_count}/{len(new_shots)} new shots, total={len(existing_shot_list)}",
    })
    return {"scene_clips": scene_clips, "plan": updated_plan, "messages": messages}


# ── Remove scene: delete shot(s) from plan ───────────────────────────────────

def _remove_scenes(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    remove_indices: list[int] = sorted(set(state.get("remove_indices", [])), reverse=True)
    project_id = state.get("project_id", "unknown")

    work_dir = Path(os.getenv("VAH_DATA_DIR", "./data")) / "projects" / project_id / "clips"

    updated_plan = copy.deepcopy(plan)
    existing_shot_list = updated_plan.get("shot_list", [])
    existing_storyboard = updated_plan.get("storyboard", [])

    # Build scene_clips
    scene_clips: list[dict] = []
    for shot in existing_shot_list:
        clip_path = work_dir / f"{shot['shot_id']}.mp4"
        scene_clips.append({
            "shot_id": shot["shot_id"],
            "clip_path": str(clip_path),
            "duration": float(shot.get("duration", 3.5)),
        })

    removed = 0
    for idx in remove_indices:
        if 0 <= idx < len(existing_shot_list):
            existing_shot_list.pop(idx)
            if idx < len(existing_storyboard):
                existing_storyboard.pop(idx)
            scene_clips.pop(idx)
            removed += 1

    # Renumber
    for i, (scene, shot, clip) in enumerate(zip(existing_storyboard, existing_shot_list, scene_clips)):
        scene["scene"] = i + 1
        shot["shot_id"] = f"S{i+1}"
        clip["shot_id"] = f"S{i+1}"

    updated_plan["storyboard"] = existing_storyboard
    updated_plan["shot_list"] = existing_shot_list
    updated_plan["duration_sec"] = round(sum(s.get("duration", 2.0) for s in existing_shot_list), 1)

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": f"[partial_executor] removed {removed} shots, {len(existing_shot_list)} remaining",
    })
    return {"scene_clips": scene_clips, "plan": updated_plan, "messages": messages}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_t2v_fn(fal_key: str | None, replicate_token: str | None):
    if fal_key:
        from render.fal_t2v import generate_clip
    else:
        from render.replicate_t2v import generate_clip
    return generate_clip
