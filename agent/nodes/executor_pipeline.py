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
    # fal.ai takes priority when both keys are set — better concurrency, no burst limits
    _provider = "fal" if fal_key else ("replicate" if replicate_token else "pil")
    print(f"[executor] provider={_provider}", file=sys.stderr, flush=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Rendering shots…", total=len(shot_list))

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
                cta_text = plan.get("script", {}).get("cta", "")

                # ── Priority 0: Outro + color variants → per-variant 1s clips ────
                # When the product has multiple color/variant images, the outro cycles
                # through each one: [color1 1s][color2 1s]…[colorN 1s]
                # Each variant: Gemini T2I ad poster → I2V (1s). Falls back gracefully.
                variant_image_paths = state.get("variant_image_paths") or []
                if is_outro and len(variant_image_paths) > 1:
                    import sys
                    from render.gemini_t2i import generate_ad_frame, build_ad_prompt
                    from agent.nodes.planner_llm import get_gemini_client
                    _gclient = get_gemini_client()
                    ad_prompt = build_ad_prompt(brand_kit, brief=state.get("brief", ""), cta_text=cta_text)
                    variant_clips: list[str] = []
                    per_variant_dur = max(1.0, round(duration / len(variant_image_paths), 1))

                    for vi, vpath in enumerate(variant_image_paths):
                        if not Path(vpath).exists():
                            continue
                        try:
                            # Gemini T2I → ad poster for this variant
                            _poster = str(work_dir / f"{shot_id}_v{vi}_ad.png")
                            if _gclient:
                                generate_ad_frame(vpath, ad_prompt, _poster, _gclient)
                            else:
                                _poster = vpath  # fallback: use raw variant image directly
                            # Static image → 1s clip (hard cut, no I2V)
                            _vout = str(work_dir / f"{shot_id}_v{vi}.mp4")
                            fc.image_to_clip(_poster, _vout, duration=per_variant_dur, ken_burns=False)
                            variant_clips.append(_vout)
                            print(f"[executor] {shot_id} variant {vi} ✓", file=sys.stderr)
                        except Exception as e:
                            print(f"[executor] {shot_id} variant {vi} failed ({e})", file=sys.stderr)

                    if len(variant_clips) >= 2:
                        # Hard cut between all variant clips
                        fc.concat_clips(variant_clips, clip_path, crossfade=0.0)
                        print(f"[executor] {shot_id}: color-variant outro ✓ ({len(variant_clips)} variants)", file=sys.stderr)
                        return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
                    # fall through to standard outro if not enough variants succeeded

                # ── Priority 1: Outro + product image → Gemini ad poster + I2V ──
                # Gemini generates a polished vertical ad poster from the product photo,
                # then I2V animates it.  Fallback chain:
                #   Gemini T2I → I2V  →  (fail)  direct product image → I2V  →  (fail)  T2V
                # The outro must NEVER fall through to concept image.
                if is_outro and product_image_path and Path(product_image_path).exists():
                    from render.gemini_t2i import generate_ad_frame, build_ad_prompt
                    from agent.nodes.planner_llm import get_gemini_client
                    _i2v_gen = (
                        __import__("render.replicate_i2v", fromlist=["generate_clip_from_image", "build_outro_motion_prompt"])
                        if _using_replicate else
                        __import__("render.fal_i2v", fromlist=["generate_clip_from_image", "build_outro_motion_prompt"])
                    )
                    generate_clip_from_image = _i2v_gen.generate_clip_from_image
                    build_outro_motion_prompt = _i2v_gen.build_outro_motion_prompt
                    motion_prompt = build_outro_motion_prompt(brand_kit, brief=state.get("brief", ""))

                    # Step A: Gemini T2I → I2V
                    _outro_img_path = product_image_path  # default to raw product image
                    try:
                        gemini_client = get_gemini_client()
                        if not gemini_client:
                            raise RuntimeError("No Gemini client available")
                        ad_img_path = str(work_dir / f"{shot_id}_ad.png")
                        ad_prompt = build_ad_prompt(
                            brand_kit,
                            brief=state.get("brief", ""),
                            cta_text=cta_text,
                        )
                        generate_ad_frame(product_image_path, ad_prompt, ad_img_path, gemini_client)
                        _outro_img_path = ad_img_path  # use Gemini-generated poster
                        import sys; print(f"[executor] {shot_id}: Gemini ad poster ✓", file=sys.stderr)
                    except Exception as e:
                        import sys
                        print(f"[executor] {shot_id}: Gemini ad poster failed ({e}) — using raw product image for I2V", file=sys.stderr)

                    # Step B: I2V with whichever image we have (Gemini poster or raw product)
                    try:
                        raw_i2v_path = str(work_dir / f"{shot_id}_i2v_raw.mp4")
                        generate_clip_from_image(_outro_img_path, motion_prompt, raw_i2v_path, quality=quality)
                        fc.trim_and_scale_clip(raw_i2v_path, clip_path, duration=duration)
                        import sys; print(f"[executor] {shot_id}: outro I2V ✓ (src={Path(_outro_img_path).name})", file=sys.stderr)
                        return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
                    except Exception as e:
                        import sys
                        print(f"[executor] {shot_id}: outro I2V also failed ({e}) — falling through to T2V", file=sys.stderr)
                        # Only now fall through to T2V — concept image is skipped for outro

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

                # ── Priority 3: show_product + product image → Gemini T2I (reference) → I2V ──
                # When the storyboard says show_product=true AND a product image is available,
                # use Gemini to generate a scene frame with the exact product, then I2V animate it.
                # This ensures product appearance is consistent with the user's reference photo.
                show_product = scene.get("show_product", False)
                if show_product and product_image_path and Path(product_image_path).exists():
                    try:
                        from render.gemini_t2i import generate_scene_frame
                        from agent.nodes.planner_llm import get_gemini_client
                        _gclient = get_gemini_client()
                        if not _gclient:
                            raise RuntimeError("No Gemini client")
                        style_tone = state.get("clarification_answers", {}).get("style_tone", ["cinematic"])
                        scene_img_path = str(work_dir / f"{shot_id}_scene.png")
                        generate_scene_frame(
                            product_image_path, desc, scene_img_path, _gclient,
                            style_tone=style_tone if isinstance(style_tone, list) else [style_tone],
                        )
                        _i2v_mod = (
                            __import__("render.replicate_i2v", fromlist=["generate_clip_from_image", "build_shot_motion_prompt"])
                            if _using_replicate else
                            __import__("render.fal_i2v", fromlist=["generate_clip_from_image", "build_shot_motion_prompt"])
                        )
                        motion_prompt = _i2v_mod.build_shot_motion_prompt(shot_type, desc, brief=state.get("brief", ""))
                        raw_path = str(work_dir / f"{shot_id}_raw.mp4")
                        _i2v_mod.generate_clip_from_image(scene_img_path, motion_prompt, raw_path, quality=quality)
                        fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
                        import sys; print(f"[executor] {shot_id}: product-ref T2I→I2V ✓", file=sys.stderr)
                        return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
                    except Exception as e:
                        import sys
                        print(f"[executor] {shot_id}: product-ref T2I→I2V failed ({e}) — falling back", file=sys.stderr)

                # ── Priority 4: Gemini concept image → I2V ───────────────────
                # If Gemini generated a concept image for this shot, use it as
                # the reference frame for I2V — videos will match the visual intent.
                concept_images = plan.get("concept_images", {})
                concept_img_data_url = concept_images.get(shot_id, "")
                if concept_img_data_url:
                    try:
                        import base64
                        # Decode data URL → PNG file
                        header, b64 = concept_img_data_url.split(",", 1)
                        img_bytes = base64.b64decode(b64)
                        concept_img_path = str(work_dir / f"{shot_id}_concept.png")
                        with open(concept_img_path, "wb") as f:
                            f.write(img_bytes)
                        # Build motion prompt from compiled T2V prompt
                        t2v_prompts = state.get("t2v_prompts", {})
                        compiled = t2v_prompts.get(shot_id, "")
                        motion_prompt = (
                            compiled.get("positive", desc) if isinstance(compiled, dict)
                            else (compiled if isinstance(compiled, str) and compiled else desc)
                        )
                        motion_prompt = (
                            f"{motion_prompt}. Animate this scene with cinematic motion. "
                            "Vertical 9:16, smooth camera movement, no text, no logos."
                        )
                        _i2v_mod = (
                            __import__("render.replicate_i2v", fromlist=["generate_clip_from_image"])
                            if _using_replicate else
                            __import__("render.fal_i2v", fromlist=["generate_clip_from_image"])
                        )
                        raw_path = str(work_dir / f"{shot_id}_raw.mp4")
                        _i2v_mod.generate_clip_from_image(concept_img_path, motion_prompt, raw_path, quality=quality)
                        fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
                        import sys; print(f"[executor] {shot_id}: Gemini I2V ✓", file=sys.stderr)
                        return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
                    except Exception as e:
                        import sys
                        print(f"[executor] {shot_id}: Gemini I2V failed ({e}) — falling back to T2V", file=sys.stderr)

                # Default: T2V — use pre-compiled prompt from PromptCompiler
                t2v_prompts = state.get("t2v_prompts", {})
                compiled = t2v_prompts.get(shot_id, "")
                negative_prompt = ""
                if isinstance(compiled, dict):
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
                    progress.advance(task)
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
                progress.advance(task)
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
