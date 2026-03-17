"""render/shot_renderer.py — single source of truth for per-shot rendering logic.

Priority chain (tried in order, each falls through on failure):
  0. Outro + multiple variant images  → Gemini T2I per variant → static clips → concat
  1. Outro + product image            → Gemini T2I ad poster  → I2V
  2. Non-outro product-type shot      → I2V from product image
  3. show_product=True shot           → Gemini T2I scene frame → I2V
  4. Gemini concept image             → I2V
  5. Default                          → T2V from compiled prompt

Both executor_pipeline and partial_executor import and call render_shot() so
the logic only ever needs to change in one place.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable


def render_shot(
    i: int,
    shot: dict,
    total_shots: int,
    work_dir: Path,
    fc: Any,                    # FFmpegComposer
    generate_clip: Callable,    # T2V function
    using_replicate: bool,
    state: dict[str, Any],
    storyboard_by_shot_id: dict[str, Any],
) -> dict[str, Any]:
    """Render a single shot and return {shot_id, clip_path, duration}."""
    shot_id = shot["shot_id"]
    duration = float(shot.get("duration", 3.5))
    clip_path = str(work_dir / f"{shot_id}.mp4")
    is_outro = (i == total_shots - 1)

    brand_kit = state.get("brand_kit", {})
    product_image_path = state.get("product_image_path", "")
    variant_image_paths = state.get("variant_image_paths") or []
    brief = state.get("brief", "")
    quality = state.get("quality", "turbo")
    plan = state.get("plan", {})
    storyboard = plan.get("storyboard", [])
    cta_text = plan.get("script", {}).get("cta", "")
    style_tone = state.get("clarification_answers", {}).get("style_tone", ["fresh"])

    scene = storyboard_by_shot_id.get(shot_id) or (storyboard[i] if i < len(storyboard) else {})
    desc = scene.get("desc") or "cinematic product shot"
    shot_type = shot.get("type", "wide")

    def _i2v_module():
        if using_replicate:
            return __import__("render.replicate_i2v", fromlist=["generate_clip_from_image", "build_shot_motion_prompt", "build_outro_motion_prompt"])
        return __import__("render.fal_i2v", fromlist=["generate_clip_from_image", "build_shot_motion_prompt", "build_outro_motion_prompt"])

    # ── Priority 0: Outro + color variants ───────────────────────────────────
    if is_outro and len(variant_image_paths) > 1:
        try:
            from render.gemini_t2i import generate_ad_frame, build_ad_prompt
            from agent.nodes.planner_llm import get_gemini_client
            _gclient = get_gemini_client()
            ad_prompt = build_ad_prompt(brand_kit, brief=brief, cta_text=cta_text)
            per_dur = max(0.1, round(1.0 / len(variant_image_paths), 3))
            variant_clips: list[str] = []

            for vi, vpath in enumerate(variant_image_paths):
                if not Path(vpath).exists():
                    continue
                try:
                    _poster = str(work_dir / f"{shot_id}_v{vi}_ad.png")
                    if _gclient:
                        generate_ad_frame(vpath, ad_prompt, _poster, _gclient)
                    else:
                        _poster = vpath
                    _vout = str(work_dir / f"{shot_id}_v{vi}.mp4")
                    fc.image_to_clip(_poster, _vout, duration=per_dur, ken_burns=False)
                    variant_clips.append(_vout)
                    print(f"[shot_renderer] {shot_id} variant {vi} ✓", file=sys.stderr)
                except Exception as e:
                    print(f"[shot_renderer] {shot_id} variant {vi} failed ({e})", file=sys.stderr)

            if len(variant_clips) >= 2:
                fc.concat_clips(variant_clips, clip_path, crossfade=0.0)
                print(f"[shot_renderer] {shot_id}: color-variant outro ✓ ({len(variant_clips)} variants)", file=sys.stderr)
                return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
        except Exception as e:
            print(f"[shot_renderer] {shot_id}: variant outro failed ({e})", file=sys.stderr)

    # ── Priority 1: Outro + product image → Gemini T2I + I2V ─────────────────
    if is_outro and product_image_path and Path(product_image_path).exists():
        try:
            from render.gemini_t2i import generate_ad_frame, build_ad_prompt
            from agent.nodes.planner_llm import get_gemini_client
            _i2v = _i2v_module()
            motion_prompt = _i2v.build_outro_motion_prompt(brand_kit, brief=brief)

            _outro_img = product_image_path
            try:
                gemini_client = get_gemini_client()
                if not gemini_client:
                    raise RuntimeError("No Gemini client")
                ad_img_path = str(work_dir / f"{shot_id}_ad.png")
                ad_prompt = build_ad_prompt(brand_kit, brief=brief, cta_text=cta_text)
                generate_ad_frame(product_image_path, ad_prompt, ad_img_path, gemini_client)
                _outro_img = ad_img_path
                print(f"[shot_renderer] {shot_id}: Gemini ad poster ✓", file=sys.stderr)
            except Exception as e:
                print(f"[shot_renderer] {shot_id}: Gemini ad poster failed ({e}) — using raw product image", file=sys.stderr)

            raw_i2v = str(work_dir / f"{shot_id}_i2v_raw.mp4")
            _i2v.generate_clip_from_image(_outro_img, motion_prompt, raw_i2v, quality=quality)
            fc.trim_and_scale_clip(raw_i2v, clip_path, duration=duration)
            print(f"[shot_renderer] {shot_id}: outro I2V ✓", file=sys.stderr)
            return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
        except Exception as e:
            print(f"[shot_renderer] {shot_id}: outro I2V failed ({e}) — falling to T2V", file=sys.stderr)

    # ── Priority 2: product-type shot + product image → I2V ──────────────────
    if shot_type == "product" and product_image_path and Path(product_image_path).exists():
        try:
            _i2v = _i2v_module()
            motion_prompt = _i2v.build_shot_motion_prompt(shot_type, desc, brief=brief)
            raw_path = str(work_dir / f"{shot_id}_raw.mp4")
            _i2v.generate_clip_from_image(product_image_path, motion_prompt, raw_path, quality=quality)
            fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
            return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
        except Exception as e:
            print(f"[shot_renderer] {shot_id}: product I2V failed ({e}) — falling to T2V", file=sys.stderr)

    # ── Priority 3: show_product=True → Gemini T2I scene frame → I2V ─────────
    if scene.get("show_product") and product_image_path and Path(product_image_path).exists():
        try:
            from render.gemini_t2i import generate_scene_frame
            from agent.nodes.planner_llm import get_gemini_client
            _gclient = get_gemini_client()
            if not _gclient:
                raise RuntimeError("No Gemini client")
            scene_img_path = str(work_dir / f"{shot_id}_scene.png")
            _style = style_tone if isinstance(style_tone, list) else [style_tone]
            generate_scene_frame(product_image_path, desc, scene_img_path, _gclient, style_tone=_style)
            _i2v = _i2v_module()
            motion_prompt = _i2v.build_shot_motion_prompt(shot_type, desc, brief=brief)
            raw_path = str(work_dir / f"{shot_id}_raw.mp4")
            _i2v.generate_clip_from_image(scene_img_path, motion_prompt, raw_path, quality=quality)
            fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
            print(f"[shot_renderer] {shot_id}: product-ref T2I→I2V ✓", file=sys.stderr)
            return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
        except Exception as e:
            print(f"[shot_renderer] {shot_id}: product-ref T2I→I2V failed ({e})", file=sys.stderr)

    # ── Priority 4: Gemini concept image → I2V ───────────────────────────────
    concept_images = plan.get("concept_images", {})
    concept_img_data_url = concept_images.get(shot_id, "")
    if concept_img_data_url:
        try:
            import base64
            _, b64 = concept_img_data_url.split(",", 1)
            concept_img_path = str(work_dir / f"{shot_id}_concept.png")
            with open(concept_img_path, "wb") as f:
                f.write(base64.b64decode(b64))
            t2v_prompts = state.get("t2v_prompts", {})
            compiled = t2v_prompts.get(shot_id, "")
            motion_prompt = (
                compiled.get("positive", desc) if isinstance(compiled, dict)
                else (compiled if isinstance(compiled, str) and compiled else desc)
            )
            motion_prompt = (
                f"{motion_prompt}. Animate with cinematic motion. "
                "Vertical 9:16, smooth camera movement, no text, no logos."
            )
            _i2v = _i2v_module()
            raw_path = str(work_dir / f"{shot_id}_raw.mp4")
            _i2v.generate_clip_from_image(concept_img_path, motion_prompt, raw_path, quality=quality)
            fc.trim_and_scale_clip(raw_path, clip_path, duration=duration)
            print(f"[shot_renderer] {shot_id}: Gemini concept I2V ✓", file=sys.stderr)
            return {"shot_id": shot_id, "clip_path": clip_path, "duration": duration}
        except Exception as e:
            print(f"[shot_renderer] {shot_id}: Gemini concept I2V failed ({e}) — falling to T2V", file=sys.stderr)

    # ── Priority 5 (default): T2V from compiled prompt ────────────────────────
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
