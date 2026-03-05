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

            quality = state.get("quality", "turbo")

            def _process_shot(args: tuple[int, dict]) -> dict:
                i, shot = args
                shot_id = shot["shot_id"]
                duration = float(shot.get("duration", 3.5))
                clip_path = str(work_dir / f"{shot_id}.mp4")

                if shot.get("type") == "text":
                    from render.fal_t2i import generate_background, build_background_prompt
                    from render.fal_i2v import generate_clip_from_image, build_outro_motion_prompt
                    is_outro = (i == len(shot_list) - 1)
                    product_image_path = state.get("product_image_path", "")
                    logo_path = brand_kit.get("logo", {}).get("path", "")
                    cta_text = shot.get("text_overlay", "")

                    if is_outro and product_image_path and Path(product_image_path).exists():
                        # Best path: FLUX Kontext → ad poster with integrated CTA/brand →
                        #            Wan I2V → subtle animated clip (no overlay sticker)
                        try:
                            from render.fal_t2i import generate_ad_frame, build_ad_prompt
                            ad_img_path = str(work_dir / f"{shot_id}_ad.png")
                            ad_prompt = build_ad_prompt(
                                brand_kit,
                                brief=state.get("brief", ""),
                                cta_text=cta_text,
                            )
                            generate_ad_frame(product_image_path, ad_prompt, ad_img_path)

                            # Animate the ad poster with subtle I2V motion
                            motion_prompt = build_outro_motion_prompt(brand_kit, brief=state.get("brief", ""))
                            raw_i2v_path = str(work_dir / f"{shot_id}_i2v_raw.mp4")
                            generate_clip_from_image(ad_img_path, motion_prompt, raw_i2v_path, quality=quality)
                            fc.trim_and_scale_clip(raw_i2v_path, clip_path, duration=duration)
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
                    else:
                        # Non-outro text shots or no product image: FLUX Schnell background + PIL compose
                        bg_path = str(work_dir / f"{shot_id}_bg.png")
                        try:
                            bg_prompt = build_background_prompt(brand_kit, is_outro=is_outro)
                            generate_background(bg_prompt, bg_path)
                        except Exception as e:
                            import sys
                            print(f"[executor] FLUX bg failed: {e} — using PIL gradient", file=sys.stderr)
                            bg_path = ""

                        fg = FrameGenerator(brand_kit=brand_kit, work_dir=work_dir)
                        frame_path = fg.generate_frame(
                            shot_id=shot_id, shot_type="text",
                            text_overlay=cta_text, scene_index=i,
                            is_outro=is_outro,
                            background_image_path=bg_path,
                            logo_path=logo_path if is_outro else "",
                        )
                        fc.image_to_clip(str(frame_path), clip_path, duration=duration,
                                         width=1080, height=1920, ken_burns=False)
                    return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}

                scene = storyboard[i] if i < len(storyboard) else {}
                desc = scene.get("desc") or "cinematic product shot"
                shot_type = shot.get("type", "wide")
                product_image_path = state.get("product_image_path", "")

                # Use I2V for product/lifestyle shots when a product image is available.
                # This eliminates AI-invented text/logos on the product — the real photo is used.
                if shot_type in ("product", "lifestyle", "close", "macro") \
                        and product_image_path and Path(product_image_path).exists():
                    try:
                        from render.fal_i2v import generate_clip_from_image, build_shot_motion_prompt
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
                tone_str = ", ".join(style_tone) if isinstance(style_tone, list) else str(style_tone)
                clean_desc = desc.replace("branded ", "").replace("brand ", "")
                prompt = (
                    f"{clean_desc}. Style: {tone_str}. "
                    "Vertical social media video, smooth motion, vibrant colors, cinematic quality. "
                    "No text overlays, no captions, no watermarks, no on-screen text, "
                    "no visible labels or writing on products, plain unbranded surfaces."
                )
                raw_path = str(work_dir / f"{shot_id}_raw.mp4")
                generate_clip(prompt, raw_path, duration=duration, quality=quality)
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
            # ── PIL fallback (no FAL_KEY) ─────────────────────────────────────
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
