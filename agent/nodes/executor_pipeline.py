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

    replicate_token = os.getenv("REPLICATE_API_TOKEN")
    fal_key = os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY")
    import sys
    _provider = "replicate" if replicate_token else ("fal" if fal_key else "pil")
    print(f"[executor] provider={_provider}", file=sys.stderr, flush=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Rendering shots…", total=len(shot_list))

        if replicate_token or fal_key:
            # ── AI T2V path (Replicate preferred, fal.ai fallback) ───────────
            if replicate_token:
                from render.replicate_t2v import generate_clip
                from render.replicate_i2v import (
                    generate_clip_from_image, build_shot_motion_prompt, build_outro_motion_prompt,
                )
                _using_replicate = True
            else:
                os.environ.setdefault("FAL_KEY", fal_key)
                from render.fal_t2v import generate_clip
                _using_replicate = False

            storyboard = plan.get("storyboard", [])
            style_tone = state.get("clarification_answers", {}).get("style_tone", ["fresh"])

            quality = state.get("quality", "turbo")

            def _process_shot(args: tuple[int, dict]) -> dict:
                i, shot = args
                shot_id = shot["shot_id"]
                duration = float(shot.get("duration", 3.5))
                clip_path = str(work_dir / f"{shot_id}.mp4")
                is_outro = (i == len(shot_list) - 1)
                product_image_path = state.get("product_image_path", "")
                logo_path = brand_kit.get("logo", {}).get("path", "")
                # CTA text: prefer shot text_overlay, fall back to plan script CTA
                cta_text = (
                    shot.get("text_overlay", "")
                    or plan.get("script", {}).get("cta", "")
                )

                # ── Priority 1: Outro + product image → FLUX Kontext ad poster + I2V ──
                # Applies regardless of shot type — ensures the last frame always
                # features the real product when the user has uploaded a photo.
                if is_outro and product_image_path and Path(product_image_path).exists():
                    from render.fal_t2i import generate_ad_frame, build_ad_prompt
                    _i2v_gen = (
                        __import__("render.replicate_i2v", fromlist=["generate_clip_from_image", "build_outro_motion_prompt"])
                        if _using_replicate else
                        __import__("render.fal_i2v", fromlist=["generate_clip_from_image", "build_outro_motion_prompt"])
                    )
                    generate_clip_from_image = _i2v_gen.generate_clip_from_image
                    build_outro_motion_prompt = _i2v_gen.build_outro_motion_prompt
                    try:
                        ad_img_path = str(work_dir / f"{shot_id}_ad.png")
                        ad_prompt = build_ad_prompt(
                            brand_kit,
                            brief=state.get("brief", ""),
                            cta_text=cta_text,
                        )
                        generate_ad_frame(product_image_path, ad_prompt, ad_img_path)
                        motion_prompt = build_outro_motion_prompt(brand_kit, brief=state.get("brief", ""))
                        raw_i2v_path = str(work_dir / f"{shot_id}_i2v_raw.mp4")
                        generate_clip_from_image(ad_img_path, motion_prompt, raw_i2v_path, quality=quality)
                        fc.trim_and_scale_clip(raw_i2v_path, clip_path, duration=duration)
                        return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
                    except Exception as e:
                        import sys
                        print(f"[executor] FLUX Kontext + I2V outro failed: {e} — falling back to PIL", file=sys.stderr)
                        fg = FrameGenerator(brand_kit=brand_kit, work_dir=work_dir)
                        frame_path = fg.generate_frame(
                            shot_id=shot_id, shot_type="text",
                            text_overlay=cta_text, scene_index=i,
                            is_outro=True, logo_path=logo_path,
                        )
                        fc.image_to_clip(str(frame_path), clip_path, duration=duration,
                                         width=1080, height=1920, ken_burns=False)
                        return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}

                # ── Priority 2: All remaining shots → T2V (includes type="text") ──
                # When no product image is available we skip static PIL cards entirely
                # and let T2V render every shot from its storyboard description.
                scene = storyboard[i] if i < len(storyboard) else {}
                desc = scene.get("desc") or "cinematic product shot"
                shot_type = shot.get("type", "wide")

                # Use I2V only for "product" type shots when a product image is available.
                # macro/lifestyle/wide shots use T2V so the storyboard scene descriptions
                # (different environments, ingredients, settings) are actually rendered.
                if shot_type == "product" \
                        and product_image_path and Path(product_image_path).exists():
                    try:
                        _i2v_mod = (
                            __import__("render.replicate_i2v", fromlist=["generate_clip_from_image", "build_shot_motion_prompt"])
                            if _using_replicate else
                            __import__("render.fal_i2v", fromlist=["generate_clip_from_image", "build_shot_motion_prompt"])
                        )
                        generate_clip_from_image = _i2v_mod.generate_clip_from_image
                        build_shot_motion_prompt = _i2v_mod.build_shot_motion_prompt
                        motion_prompt = build_shot_motion_prompt(
                            shot_type, desc, brief=state.get("brief", "")
                        )
                        raw_path = str(work_dir / f"{shot_id}_raw.mp4")
                        generate_clip_from_image(product_image_path, motion_prompt, raw_path, quality=quality)
                        fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
                        return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
                    except Exception as e:
                        import sys
                        print(f"[executor] I2V shot failed: {e} — falling back to T2V", file=sys.stderr)

                # Default: T2V for non-product shots or when no product image is available
                # Use pre-compiled prompt from PromptCompiler if available
                t2v_prompts = state.get("t2v_prompts", {})
                compiled = t2v_prompts.get(shot_id, "")
                negative_prompt = ""
                if isinstance(compiled, dict):
                    # New format: {positive, negative}
                    prompt = compiled.get("positive", "")
                    negative_prompt = compiled.get("negative", "")
                elif isinstance(compiled, str) and compiled:
                    prompt = compiled
                else:
                    tone_str = ", ".join(style_tone) if isinstance(style_tone, list) else str(style_tone)
                    clean_desc = desc.replace("branded ", "").replace("brand ", "")
                    prompt = (
                        f"{clean_desc}. Style: {tone_str}. "
                        "Vertical social media video, smooth motion, vibrant colors, cinematic quality. "
                        "No text overlays, no captions, no watermarks, no on-screen text, "
                        "no visible labels or writing on products, plain unbranded surfaces."
                    )
                raw_path = str(work_dir / f"{shot_id}_raw.mp4")
                generate_clip(prompt, raw_path, duration=duration, quality=quality, negative_prompt=negative_prompt)
                fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
                return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}

            results: dict[int, dict] = {}
            with ThreadPoolExecutor(max_workers=min(6, len(shot_list))) as pool:
                futures = {pool.submit(_process_shot, (i, s)): i for i, s in enumerate(shot_list)}
                for fut in as_completed(futures):
                    idx = futures[fut]
                    results[idx] = fut.result()
                    progress.advance(task)
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
                    text_overlay=shot.get("text_overlay", ""),
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
                progress.advance(task)

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[executor] {len(scene_clips)} clips rendered to {work_dir}",
        }
    )

    return {"scene_clips": scene_clips, "messages": messages}
