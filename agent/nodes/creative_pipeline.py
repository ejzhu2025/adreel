"""creative_pipeline — Director → Storyboard → Critic → PromptCompiler.

Four focused LLM calls replace the monolithic planner_llm prompt.
Each step has a deterministic mock fallback so the pipeline runs without an API key.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

from rich.console import Console

console = Console()

# ── Type alias ────────────────────────────────────────────────────────────────

LLMCall = Callable[[str, str], str]  # (system_prompt, user_prompt) -> raw text


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Director
# ══════════════════════════════════════════════════════════════════════════════

DIRECTOR_SYSTEM = """\
You are a creative director for short-form social media videos.
Given a brief and brand context, generate 3 DISTINCT creative concepts, then select the best one.

Output ONLY valid JSON (no markdown fences):
{
  "concepts": [
    {
      "id": "C1",
      "hook_angle": "<hook strategy, e.g. 'sensory immersion', 'problem-solution', 'surprise reveal'>",
      "visual_style": "<cinematography style, e.g. 'macro textures + golden-hour lifestyle'>",
      "key_message": "<core message in 1 sentence>",
      "mood": "<emotional tone, e.g. 'energetic', 'serene', 'playful', 'luxurious'>",
      "scene_count": <int 3-6>,
      "visual_signature": {
        "camera_style": "<single unified camera movement for ALL shots, e.g. 'locked-off macro — NO handheld mixing'>",
        "color_palette": "<2-3 hex codes + descriptive names, e.g. '#00B894 deep teal, #FFE082 warm straw, #FFFFFF clean white'>",
        "lighting": "<unified lighting setup for all shots, e.g. 'harsh overhead midday sun with sharp drop shadows'>",
        "visual_motif": "<repeating visual element that threads through all shots, e.g. 'condensation water droplets on cup exterior'>"
      }
    },
    { ... concept 2 ... },
    { ... concept 3 ... }
  ],
  "best_index": <0-based index of the concept that best fits this brief and brand>
}

Rules:
- Each concept must differ meaningfully in hook_angle, visual_style, and mood
- Choose best_index based on: brand identity fit, platform context (TikTok/Reels/Shorts), brief goals
- scene_count guides the storyboard (not a hard limit)
- visual_signature defines the unbreakable visual language for the entire film — all shots must conform
- camera_style must be ONE style only (no mixing); color_palette must appear in every non-text shot

CRITICAL — T2V generability constraint:
All concepts MUST describe scenes that exist in the real physical world and can be captured by a camera.
FORBIDDEN visual_style and camera_style patterns (these are post-production VFX, not T2V-generatable):
- split-screen / divided frame / left-half right-half with different lighting
- color gel lighting that divides the frame into separate zones
- graphic overlays / halftone dots / pop-art graphic elements
- collision dissolve / two colors crashing together / color flood / color explosion
- wipe transitions / screen wipes / split-line animations
- digital zoom / digital push (in-camera only, no post-zoom)
- After Effects / motion graphics / animated text / kinetic typography
- any effect that requires compositing two separate video layers

ALLOWED styles: macro, lifestyle, wide, close-up, product shot, slow-motion, time-lapse,
overhead flat-lay, handheld, steadicam, dolly, crane — anything a real camera can capture in one take.
"""

DIRECTOR_USER_TEMPLATE = """\
Brief: {brief}
Brand: {brand_name}, primary color: {primary_color}, CTA: "{outro_cta}"
Platform: {platform}, Duration: {duration_sec}s, Language: {language}
Tone preference: {style_tone}

Generate 3 creative concepts and select the best one.
"""


def run_director(state: dict[str, Any], llm_call: LLMCall) -> dict[str, Any]:
    """Step 1: generate 3 concepts, return the best one."""
    brand_kit = state.get("brand_kit", {})
    answers = state.get("clarification_answers", {})

    user_msg = DIRECTOR_USER_TEMPLATE.format(
        brief=state.get("brief", ""),
        brand_name=brand_kit.get("name", brand_kit.get("brand_id", "Brand")),
        primary_color=brand_kit.get("colors", {}).get("primary", "#00B894"),
        outro_cta=brand_kit.get("intro_outro", {}).get("outro_cta", "Order now"),
        platform=answers.get("platform", "tiktok"),
        duration_sec=int(answers.get("duration_sec", 20)),
        language=answers.get("language", "en"),
        style_tone=answers.get("style_tone", ["fresh"]),
    )

    try:
        with console.status("[cyan][director] Generating creative concepts…[/cyan]"):
            raw = llm_call(DIRECTOR_SYSTEM, user_msg)
        data = _parse_json(raw)
        concepts = data.get("concepts", [])
        best_idx = int(data.get("best_index", 0))
        best = concepts[best_idx] if concepts else {}
        console.print(
            f"[green][director][/green] Selected concept {best.get('id', '?')}: "
            f"{best.get('hook_angle', '')} / {best.get('mood', '')}"
        )
        return best
    except Exception as e:
        console.print(f"[yellow][director] Error: {e} — using mock concept[/yellow]")
        return _mock_concept(state)


def _mock_concept(state: dict[str, Any]) -> dict[str, Any]:
    brief = state.get("brief", "").lower()
    is_food = any(w in brief for w in ("tong sui", "coconut", "watermelon", "drink", "food"))
    brand_kit = state.get("brand_kit", {})
    primary_color = brand_kit.get("colors", {}).get("primary", "#00B894")
    return {
        "id": "C1",
        "hook_angle": "sensory immersion" if is_food else "problem-solution",
        "visual_style": "macro textures + golden-hour lifestyle" if is_food else "clean product reveal + lifestyle",
        "key_message": "Experience summer in every sip" if is_food else "A product you'll love",
        "mood": "fresh and energetic",
        "scene_count": 4,
        "visual_signature": {
            "camera_style": "locked-off macro with slow push-in — NO handheld mixing",
            "color_palette": f"{primary_color} deep teal, #FFE082 warm straw, #FFFFFF clean white",
            "lighting": "soft natural window light with warm golden highlights",
            "visual_motif": "condensation water droplets on cup exterior" if is_food else "clean product surface reflections",
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Storyboard
# ══════════════════════════════════════════════════════════════════════════════

STORYBOARD_SYSTEM = """\
You are an expert short-form video storyboard artist and script writer.
Given a creative concept, brand kit, and brief, produce a complete video plan as valid JSON.

Output ONLY a single JSON object starting with { — do NOT wrap it in an array [ ].
Schema (no markdown fences):
{
  "project_id": "<string>",
  "brief": "<string>",
  "platform": "<tiktok|reels|shorts>",
  "duration_sec": <int>,
  "language": "<en|zh>",
  "style_tone": ["<tone>"],
  "script": {
    "hook": "<opening line, 5-10 words, grabs attention>",
    "body": ["<line1>", "<line2>", "<line3>"],
    "cta": "<call to action, 5-10 words>"
  },
  "storyboard": [
    {
      "scene": 1,
      "desc": "<visual description — must echo color_palette and camera_style from visual_signature>",
      "duration": <float>,
      "asset_hint": "<macro|lifestyle|text|product>",
      "narrative_beat": "<hook|tease|build|reveal|climax|payoff>",
      "transition_in": "<how this shot connects from the previous one, e.g. 'opening — establish color palette' or 'match cut — continue push-in, echo teal from S1'>"
    },
    ...
  ],
  "shot_list": [
    {"shot_id": "S1", "type": "<macro|wide|close|text|transition>", "asset": "<asset key or 'generate'>", "text_overlay": "<text>", "duration": <float>},
    ...
  ],
  "render_targets": ["9:16"]
}

Rules:
- Use the creative concept's scene_count as guide; adjust based on duration and user requirements
- Each shot duration must be between 0.5 and 2.0 seconds
- shot_list must have one entry per storyboard scene (S1, S2, … SN)
- Hook is always scene 1 with narrative_beat "hook"; follow the concept's hook_angle strategy
- Match the concept's visual_style and mood throughout
- Keep text_overlay short (max 8 words)
- duration_sec should equal the sum of all shot durations
- Every scene's desc MUST reflect the visual_signature: use the color_palette colors, camera_style movement, and visual_motif
- narrative_beat must follow a coherent arc: hook → tease/build → reveal/climax → payoff
- transition_in for scene 1 must be "opening — establish color palette and camera style"
- transition_in for subsequent scenes must describe a specific visual link to the previous scene (color echo, subject match, motion continuation)

CRITICAL — feedback / modification rules:
- Follow the user's modification request EXACTLY and LITERALLY
- If the user says "modify scene N" or "change scene N to X": edit that scene's desc/type/text_overlay in place
- If the user says "add a scene between scene N and scene N+1": INSERT a new scene, renumber subsequent scenes
- If the user says "remove scene N": DELETE that scene, renumber subsequent scenes
- If the user says "add X to scene N": update scene N's desc to include X — do NOT insert a new scene
- If the modification request is AMBIGUOUS or UNCLEAR, do NOT guess — output:
  {"clarification_needed": true, "question": "<specific question to ask the user>"}

CRITICAL — storyboard desc rules:
- "desc" fields describe ONLY visual scene elements: motion, lighting, composition, colors, environment, camera angle, textures
- NEVER mention text, captions, logos, watermarks, overlays, written words, taglines, slogans, or any on-screen graphics in desc
- NEVER use words like: "text appears", "logo shown", "caption", "title card", "CTA", "branded graphic", "overlay", "tagline", "branded cup", "branded bottle", "branding"
- NEVER use the word "branded" — say "product cup", "product bottle", "the drink" instead
- Text content belongs EXCLUSIVELY in "text_overlay" fields, never in "desc"
- An outro (type "text") is optional — only include one if the user explicitly wants a brand card
- If included, "text" type shots must have desc describing an abstract visual background only (colors, mood, bokeh)
- shot_list must correspond 1-to-1 with storyboard scenes — no extra shots, no missing shots
"""

STORYBOARD_USER_TEMPLATE = """\
Brief: {brief}
Brand: {brand_name}, primary color: {primary_color}, CTA: "{outro_cta}"
Platform: {platform}, Duration: {duration_sec}s, Language: {language}
Tone: {style_tone}

Creative Concept:
- Hook angle: {hook_angle}
- Visual style: {visual_style}
- Key message: {key_message}
- Mood: {mood}
- Recommended scene count: {scene_count}

Visual Signature (MANDATORY — apply to every non-text scene):
- Camera style: {vs_camera_style}
- Color palette: {vs_color_palette}
- Lighting: {vs_lighting}
- Visual motif: {vs_visual_motif}

User feedback / modification request: {feedback}
Similar past projects (for reference): {similar_projects}
{existing_plan_block}
Generate the storyboard JSON now.
"""


def run_storyboard(
    state: dict[str, Any],
    concept: dict[str, Any],
    project_id: str,
    llm_call: LLMCall,
) -> dict[str, Any]:
    """Step 2: expand concept into a full storyboard plan."""
    brand_kit = state.get("brand_kit", {})
    answers = state.get("clarification_answers", {})
    feedback = state.get("plan_feedback", "")
    similar = state.get("similar_projects", [])
    style_tone = answers.get("style_tone", ["fresh"])
    if isinstance(style_tone, str):
        style_tone = [style_tone]

    existing_plan = state.get("plan")
    if feedback and existing_plan:
        existing_plan_block = (
            "Current plan to modify (apply the feedback above to THIS plan):\n"
            + json.dumps(existing_plan, ensure_ascii=False, indent=2)
        )
    else:
        existing_plan_block = ""

    vs = concept.get("visual_signature", {})
    user_msg = STORYBOARD_USER_TEMPLATE.format(
        brief=state.get("brief", ""),
        brand_name=brand_kit.get("name", brand_kit.get("brand_id", "Brand")),
        primary_color=brand_kit.get("colors", {}).get("primary", "#00B894"),
        outro_cta=brand_kit.get("intro_outro", {}).get("outro_cta", "Order now"),
        platform=answers.get("platform", "tiktok"),
        duration_sec=int(answers.get("duration_sec", 20)),
        language=answers.get("language", "en"),
        style_tone=", ".join(style_tone),
        hook_angle=concept.get("hook_angle", "sensory immersion"),
        visual_style=concept.get("visual_style", "cinematic"),
        key_message=concept.get("key_message", ""),
        mood=concept.get("mood", "fresh"),
        scene_count=concept.get("scene_count", 4),
        vs_camera_style=vs.get("camera_style", "locked-off — no handheld"),
        vs_color_palette=vs.get("color_palette", "#00B894 teal, #FFE082 straw, #FFFFFF white"),
        vs_lighting=vs.get("lighting", "soft natural window light"),
        vs_visual_motif=vs.get("visual_motif", "consistent product surface"),
        feedback=feedback or "None",
        similar_projects=json.dumps(
            [s.get("document", "") for s in similar[:2]], ensure_ascii=False
        ),
        existing_plan_block=existing_plan_block,
    )

    try:
        with console.status("[cyan][storyboard] Generating storyboard…[/cyan]"):
            raw = llm_call(STORYBOARD_SYSTEM, user_msg)
        plan = _parse_json(raw)
        # Guard: LLM sometimes wraps the plan in a JSON array
        if isinstance(plan, list):
            # Case 1: array wraps a single plan object
            wrapped = next(
                (item for item in plan if isinstance(item, dict) and "storyboard" in item),
                None,
            )
            if wrapped:
                plan = wrapped
            else:
                # Case 2: array IS the storyboard scene list — try to reassemble
                scenes = [item for item in plan if isinstance(item, dict) and "scene" in item]
                if scenes:
                    console.print("[yellow][storyboard] LLM returned bare scene array — reassembling plan[/yellow]")
                    answers = state.get("clarification_answers", {})
                    style_tone = answers.get("style_tone", ["fresh"])
                    if isinstance(style_tone, str):
                        style_tone = [style_tone]
                    plan = {
                        "platform": answers.get("platform", "tiktok"),
                        "duration_sec": sum(s.get("duration", 1.0) for s in scenes),
                        "language": answers.get("language", "en"),
                        "style_tone": style_tone,
                        "script": {"hook": "", "body": [], "cta": ""},
                        "storyboard": scenes,
                        "shot_list": [
                            {
                                "shot_id": f"S{s['scene']}",
                                "type": s.get("asset_hint", "lifestyle"),
                                "asset": "generate",
                                "text_overlay": "",
                                "duration": s.get("duration", 1.0),
                            }
                            for s in scenes
                        ],
                        "render_targets": ["9:16"],
                    }
                else:
                    raise ValueError("LLM returned a JSON array with no recoverable plan data")
        plan["project_id"] = project_id
        shot_count = len(plan.get("shot_list", []))
        console.print(f"[green][storyboard][/green] {shot_count} shots generated")
        return plan
    except Exception as e:
        console.print(f"[yellow][storyboard] Error: {e} — using mock plan[/yellow]")
        console.print(f"[dim][storyboard] Raw response (first 400 chars): {raw[:400] if 'raw' in dir() else 'N/A'}[/dim]")
        from agent.nodes.planner_llm import _mock_plan

        answers_copy = state.get("clarification_answers", {})
        return _mock_plan(
            state,
            project_id,
            answers_copy.get("platform", "tiktok"),
            int(answers_copy.get("duration_sec", 20)),
            answers_copy.get("language", "en"),
            style_tone,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Critic
# ══════════════════════════════════════════════════════════════════════════════

CRITIC_SYSTEM = """\
You are a quality-control critic for short-form video storyboards.
Review the given storyboard JSON and output corrections as RFC 6902 JSON Patch.

Check for these issues:

1. desc fields containing forbidden branding/overlay words: "branded", "branding", "logo shown",
   "text appears", "caption", "title card", "watermark", "overlay", "tagline", "slogan", "CTA"

2. "text" type shots whose desc is not an abstract visual (must be only colors/mood/bokeh)

3. shot_list and storyboard length mismatch (must be 1-to-1)

4. Shot durations outside [0.5, 2.0] range (clamp to nearest bound)

5. Missing text_overlay for the last shot when it is type "text"

6. T2V GENERABILITY — desc describes post-production VFX that a video model cannot render.
   Trigger phrases (any of these → must patch):
     split-screen, split screen, divided frame, left half / right half (with different lighting)
     color gel, gel-lit, gel light, color zone, colored lighting on one side
     halftone, pop-art graphic, graphic element, graphic overlay
     collision dissolve, colors crash together, color flood, color explosion, color burst
     split line, dividing line, line pulses, line wobbles, line cracks
     wipe transition, screen wipe, vertical wipe, horizontal wipe
     digital zoom, digital push-in (post-production zoom is forbidden; camera dolly is allowed)
     After Effects, motion graphics, animated, kinetic, compositing

   When patching a generability violation, rewrite the desc to describe the SAME EMOTIONAL BEAT
   using only real-world cinematography: macro textures, product close-up, lifestyle action,
   lighting effects (practicals, window light, studio softbox), camera movement (dolly, push-in).
   Keep duration, narrative_beat, and asset_hint unchanged — only replace desc.

   Example fix:
   BAD:  "vertical split-screen: left half red gel light with watermelon, right half white with coconut"
   GOOD: "Overhead macro shot, halved watermelon and cracked coconut placed side by side on clean
          white surface. Bright flat studio softbox lighting. Both subjects fill the frame symmetrically."

Output ONLY a JSON array of RFC 6902 patch operations (no markdown fences):
[
  {"op": "replace", "path": "/storyboard/0/desc", "value": "corrected visual description"},
  ...
]

If no issues are found, output: []

IMPORTANT: Only patch actual problems. Do not change things that are already correct.
"""

CRITIC_USER_TEMPLATE = """\
Review this storyboard and output JSON Patch corrections:

{storyboard_json}
"""


# ── Generability detector (Python-level, no LLM needed) ──────────────────────

# Patterns that indicate post-production VFX — T2V models cannot render these
_VFX_PATTERNS = re.compile(
    r"split[\s-]screen|split\s+line|divid(?:ed|ing)\s+frame"
    r"|left\s+half|right\s+half"
    r"|color\s+gel|gel[\s-]lit|gel\s+light|colou?red?\s+lighting\s+on\s+one\s+side"
    r"|halftone|pop[\s-]art\s+graphic|graphic\s+element|graphic\s+overlay"
    r"|collision\s+dissolve|colou?rs?\s+crash|color\s+flood|colou?r\s+explosion|colou?r\s+burst"
    r"|split\s+line\s+(?:pulses?|wobbles?|cracks?)|dividing\s+line"
    r"|wipe\s+transition|screen\s+wipe|vertical\s+wipe|horizontal\s+wipe"
    r"|digital\s+zoom|digital\s+push"
    r"|after\s+effects|motion\s+graphics|kinetic\s+typograph",
    re.IGNORECASE,
)

_REWRITE_SYSTEM = """\
You are rewriting a single storyboard scene description to be T2V-generatable.
The original desc contains post-production VFX effects that a video generation model cannot render.

Rules:
- Keep the same EMOTIONAL BEAT and narrative function (hook/build/climax/payoff)
- Keep the same subject matter (product, food, lifestyle)
- Replace VFX with real-world cinematography: macro close-up, product shot, overhead flat-lay,
  lifestyle action, dolly/push-in camera move, studio softbox or natural light
- Keep desc concise (2-4 sentences)
- Do NOT mention split-screen, gel light, color zones, collision, dissolve, wipe, or any VFX
- Output ONLY the rewritten desc string — no JSON, no explanation
"""


def _rewrite_desc(desc: str, beat: str, shot_type: str, llm_call: LLMCall) -> str:
    """Ask LLM to rewrite a single non-generatable desc into a T2V-safe one."""
    user_msg = (
        f"narrative_beat: {beat}\n"
        f"shot_type: {shot_type}\n"
        f"original desc (contains VFX — rewrite it):\n{desc}"
    )
    try:
        result = llm_call(_REWRITE_SYSTEM, user_msg).strip().strip('"')
        return result
    except Exception:
        return desc  # fallback: keep original if rewrite fails


def run_critic(plan: dict[str, Any], llm_call: LLMCall) -> dict[str, Any]:
    """Step 3: review storyboard and apply JSON Patch corrections.

    Two-pass approach:
    - Pass A (Python): deterministically detect T2V generability violations, rewrite via LLM
    - Pass B (LLM):    check remaining issues (forbidden words, duration, type mismatch, etc.)
    """
    import copy as _copy
    plan = _copy.deepcopy(plan)

    # ── Pass A: generability check ────────────────────────────────────────────
    storyboard = plan.get("storyboard", [])
    shot_list = plan.get("shot_list", [])
    violations: list[int] = []
    for i, scene in enumerate(storyboard):
        if _VFX_PATTERNS.search(scene.get("desc", "")):
            violations.append(i)

    if violations:
        console.print(f"[yellow][critic][/yellow] {len(violations)} generability violation(s) detected — rewriting…")
        for i in violations:
            scene = storyboard[i]
            shot_type = shot_list[i].get("type", "lifestyle") if i < len(shot_list) else "lifestyle"
            new_desc = _rewrite_desc(
                scene["desc"],
                scene.get("narrative_beat", ""),
                shot_type,
                llm_call,
            )
            storyboard[i]["desc"] = new_desc
            console.print(f"  [green]S{scene['scene']}[/green] rewritten")
    else:
        console.print("[dim][critic] No generability violations[/dim]")

    # ── Pass B: LLM quality check ─────────────────────────────────────────────
    user_msg = CRITIC_USER_TEMPLATE.format(
        storyboard_json=json.dumps(plan, ensure_ascii=False, indent=2)
    )
    try:
        with console.status("[cyan][critic] QC check…[/cyan]"):
            raw = llm_call(CRITIC_SYSTEM, user_msg)
        patch_ops = _parse_json(raw)
        if not isinstance(patch_ops, list):
            patch_ops = []
        if patch_ops:
            console.print(f"[green][critic][/green] Applying {len(patch_ops)} QC fix(es)")
            plan = _apply_patch(plan, patch_ops)
        else:
            console.print("[green][critic][/green] QC passed")
    except Exception as e:
        console.print(f"[yellow][critic] QC error: {e} — skipping[/yellow]")

    return plan


def _apply_patch(obj: Any, patch: list[dict]) -> Any:
    """Apply RFC 6902 JSON Patch operations (replace / add / remove)."""
    result = copy.deepcopy(obj)
    for op in patch:
        operation = op.get("op", "")
        path = op.get("path", "")
        value = op.get("value")
        try:
            parts: list[str | int] = []
            for p in path.strip("/").split("/"):
                parts.append(int(p) if p.isdigit() else p)
            if not parts:
                continue
            target: Any = result
            for part in parts[:-1]:
                target = target[part]
            key: str | int = parts[-1]
            if operation in ("replace", "add"):
                target[key] = value
            elif operation == "remove":
                if isinstance(target, list):
                    del target[int(key)]
                else:
                    target.pop(key, None)
        except Exception:
            pass  # skip bad ops silently
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Step 4 — PromptCompiler
# ══════════════════════════════════════════════════════════════════════════════

COMPILER_SYSTEM = """\
You are a prompt engineer specializing in Wan T2V / I2V video generation models.
Given a storyboard with visual signature and cross-shot context, write one optimized prompt per shot.

Output ONLY valid JSON (no markdown fences):
{
  "S1": {"positive": "<English positive prompt>", "negative": "<negative prompt>"},
  "S2": {"positive": "...", "negative": "..."},
  ...
}

━━ FORMAT BY SHOT TYPE ━━

"text" type
→ {"positive": "", "negative": ""}  (rendered by PIL, never sent to the AI model)

"product" type  (I2V — reference image provided at runtime)
→ positive: describe ONLY the motion. Do NOT describe product appearance.
  Template: "[motion_speed] [gentle motion verb] the product, [secondary motion detail]. [lighting glint on surface]. Vertical 9:16, locked camera, no shake."
  Example: "Ultra-slow creep, gentle 360° rotation of the bottle, soft liquid shimmer ripples across surface. Studio rim light glints across the cap edge. Vertical 9:16, locked camera, no shake."
→ negative: "camera shake, zoom, pan, tilt, handheld jitter, text, captions, watermark, logo"

"macro" type  (T2V — extreme close-up of texture/material/liquid)
→ positive structure (flowing prose, not bullet points):
  "[motion_speed] Extreme close-up macro shot, [micro-texture subject + physical motion].
   [single point light source + how it hits the surface]. [color_name] tones, [visual_motif].
   Vertical 9:16 video, [style_keywords], no text, no captions, no logos."
→ negative: "wide shot, full body, blurry, low detail, shaky camera, text, captions, watermark, logo, CGI artifacts"

"lifestyle" type  (T2V — person in environment)
→ positive structure:
  "[motion_speed] [shot_size] shot, [person + posture + micro-expression/emotion].
   [environment + setting + background depth]. [light quality + direction], [color_name] ambient tones. [visual_motif].
   Vertical 9:16 video, [style_keywords], no text, no captions, no logos."
→ negative: "plastic skin, uncanny valley, bad anatomy, extra limbs, distorted face, text, captions, watermark, logo"

"wide" type  (T2V — establishing space)
→ positive structure:
  "[motion_speed] Wide establishing shot, [scene composition — foreground/midground/background].
   [main subject in space + environmental motion]. [light + color_name].
   Vertical 9:16 video, [style_keywords], no text, no captions, no logos."
→ negative: "extreme close-up, macro, portrait crop, shaky, handheld, text, captions, watermark, logo"

"close" type  (T2V — tight detail shot, face/hands/material)
→ positive structure:
  "[motion_speed] Tight close-up, [subject detail — skin/material texture + micro-movement].
   [single point light + shadow depth], [color_name] tones, background fully defocused.
   Vertical 9:16 video, [style_keywords], no text, no captions, no logos."
→ negative: "wide shot, full body, sharp background, text, captions, watermark, logo, distorted face, bad skin"

"transition" type  (T2V)
→ positive: brief motion bridge, same style rules as surrounding shots
→ negative: standard negative (text, captions, watermark, logo, abrupt cut)

━━ CROSS-SHOT COHERENCE (MANDATORY) ━━
1. ALWAYS write in English regardless of brief language (translate Chinese desc)
2. Use color descriptive NAMES from color_palette in every non-text positive prompt (never hex codes)
3. Mirror the camera_style movement in every non-text shot
4. Reference the visual_motif in macro/lifestyle/close shots
5. Use prev_desc and prev_beat to echo subject position, color, or motion direction
6. If transition_in says "match cut", open positive prompt with a matching element from prev_desc
7. Use motion_speed (pre-calculated from shot duration) as the opening speed qualifier

━━ CRITICAL RULES ━━
- Output language: ENGLISH ONLY
- positive prompt: 60–100 words per shot
- negative prompt: comma-separated keywords, 10–20 terms, no full sentences
- NEVER mention text, captions, logos, overlays, written words in any positive prompt
- NEVER use "branded" — say "product bottle", "the drink", "the jar"
"""

COMPILER_USER_TEMPLATE = """\
Style/mood: {mood}, {visual_style}
Style keywords for this brand: {style_keywords}
Tone: {style_tone}

Visual Signature — embed ALL of these in EVERY non-text shot:
  camera_style : {vs_camera_style}
  color_palette: {vs_color_palette_named}   ← these are pre-translated color names, use them verbatim
  lighting     : {vs_lighting}
  visual_motif : {vs_visual_motif}

Shot sequence (motion_speed is pre-calculated from duration — use it as the opening speed qualifier):
{shots_json}

Write one positive+negative prompt pair per shot_id. Output JSON only.
"""


# ── #5: Hex → descriptive color name ─────────────────────────────────────────

# Common marketing palette hex → human-readable name
_HEX_NAME_MAP: dict[str, str] = {
    "#4a7c59": "deep forest green", "#00b894": "fresh teal",
    "#d4e8d0": "mint mist",         "#4caf50": "vivid green",
    "#c9a84c": "liquid gold",       "#ffe082": "warm straw",
    "#ff9a00": "amber gold",        "#ffd700": "bright gold",
    "#ff4500": "flame orange",      "#ff6b6b": "coral red",
    "#e74c3c": "bold red",          "#ff5722": "deep orange",
    "#00e5ff": "neon cyan",         "#2196f3": "electric blue",
    "#1a1a2e": "deep midnight navy","#0d0d0d": "near black",
    "#1a1a1a": "deep charcoal",     "#1a1008": "deep obsidian brown",
    "#000000": "pure black",        "#ffffff": "clean white",
    "#f5f5f5": "cool white",        "#f5ecd7": "ivory cream",
    "#f5efe0": "oat white",         "#f0e6d3": "film oat beige",
    "#d4c5a9": "morning mist beige","#d4b896": "warm clay",
    "#c8b99a": "vintage warm brown","#f5edd6": "pale ivory",
}


def _hex_to_name(hex_color: str) -> str:
    """Convert a hex color string to a descriptive name for use in T2V prompts."""
    normalized = hex_color.strip().lower()
    if not normalized.startswith("#"):
        normalized = "#" + normalized
    if normalized in _HEX_NAME_MAP:
        return _HEX_NAME_MAP[normalized]
    # Fallback: rough HSL bucket
    try:
        r = int(normalized[1:3], 16) / 255
        g = int(normalized[3:5], 16) / 255
        b = int(normalized[5:7], 16) / 255
        mx, mn = max(r, g, b), min(r, g, b)
        lightness = (mx + mn) / 2
        if mx == mn:
            return "neutral gray" if lightness > 0.4 else "deep charcoal"
        saturation = (mx - mn) / (1 - abs(2 * lightness - 1))
        hue = 0.0
        if mx == r:
            hue = (g - b) / (mx - mn) % 6
        elif mx == g:
            hue = (b - r) / (mx - mn) + 2
        else:
            hue = (r - g) / (mx - mn) + 4
        hue_deg = hue * 60
        if lightness < 0.15:
            return "deep black"
        if lightness > 0.9:
            return "bright white"
        if saturation < 0.15:
            return "warm beige" if lightness > 0.7 else "neutral gray"
        if hue_deg < 30 or hue_deg >= 330:
            return "rich red"
        if hue_deg < 60:
            return "warm orange"
        if hue_deg < 90:
            return "golden yellow"
        if hue_deg < 150:
            return "fresh green"
        if hue_deg < 200:
            return "cool teal"
        if hue_deg < 260:
            return "deep blue"
        if hue_deg < 300:
            return "vivid purple"
        return "warm magenta"
    except Exception:
        return hex_color  # return as-is if parsing fails


def _translate_palette(palette_str: str) -> str:
    """Replace hex codes in a color palette string with descriptive names.

    Input:  "#4A7C59 深竹绿, #F5ECD7 奶白麦色"
    Output: "deep forest green, ivory cream"
    """
    import re
    parts = [p.strip() for p in palette_str.split(",")]
    named: list[str] = []
    for part in parts:
        hex_match = re.search(r"#[0-9a-fA-F]{6}", part)
        if hex_match:
            named.append(_hex_to_name(hex_match.group(0)))
        else:
            # No hex — strip Chinese characters, keep English descriptor if any
            english = re.sub(r"[\u4e00-\u9fff·。，\s]+", " ", part).strip()
            named.append(english if english else part.strip())
    return ", ".join(n for n in named if n)


# ── #2: Duration → motion speed qualifier ────────────────────────────────────

def _duration_to_motion_speed(duration: float) -> str:
    """Map shot duration to a motion speed keyword for T2V prompts."""
    if duration <= 1.0:
        return "snap-cut speed, instant reveal"
    if duration <= 1.5:
        return "brisk pace, quick push-in"
    if duration <= 2.5:
        return "steady moderate pace"
    if duration <= 4.0:
        return "slow deliberate glide"
    return "ultra-slow creep, barely perceptible drift"


# ── #4: Mood → style keyword expansion ───────────────────────────────────────

_MOOD_STYLE_KEYWORDS: dict[str, str] = {
    "serene":       "dreamy, soft bokeh, gentle film grain, muted tones, ethereal haze",
    "luxurious":    "jewelry-ad lighting, dramatic chiaroscuro, hyper-detailed texture, dark opulence",
    "fresh":        "bright airy, clean whites, vivid saturation, sunlit",
    "energetic":    "high contrast, saturated colors, motion blur streaks, kinetic energy",
    "playful":      "warm pastel tones, bouncy motion, cheerful lighting",
    "热血":         "high contrast, neon rim light, motion blur streaks, kinetic energy",
    "动感炸裂":     "overcranked slow-motion, light trails, explosive particle burst, neon glow",
    "温柔治愈":     "soft pastel, warm diffused light, dreamy bokeh, gentle film grain",
    "luxurious · 神秘 · 震撼": "jewelry-ad lighting, dramatic chiaroscuro, dark opulence, hyper-detailed",
    "奢华 · 神秘 · 震撼":      "jewelry-ad lighting, dramatic chiaroscuro, dark opulence, hyper-detailed",
}

def _mood_to_style_keywords(mood: str) -> str:
    mood_lower = mood.lower().strip()
    for key, val in _MOOD_STYLE_KEYWORDS.items():
        if key.lower() in mood_lower or mood_lower in key.lower():
            return val
    return "cinematic quality, smooth motion, professional color grade"


# ── Cross-shot sequence builder ───────────────────────────────────────────────

def _build_cross_shot_sequence(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge storyboard scenes with shot_list entries.

    Injects: prev_desc, prev_beat, motion_speed (from duration).
    """
    storyboard = plan.get("storyboard", [])
    shot_list = plan.get("shot_list", [])
    scenes_by_idx = {i: s for i, s in enumerate(storyboard)}
    shots: list[dict[str, Any]] = []
    for i, shot in enumerate(shot_list):
        scene = scenes_by_idx.get(i, {})
        duration = float(shot.get("duration", 1.0))
        entry: dict[str, Any] = {
            "shot_id": shot.get("shot_id", f"S{i+1}"),
            "type": shot.get("type", ""),
            "desc": scene.get("desc", ""),
            "narrative_beat": scene.get("narrative_beat", ""),
            "transition_in": scene.get("transition_in", ""),
            "text_overlay": shot.get("text_overlay", ""),
            "duration": duration,
            "motion_speed": _duration_to_motion_speed(duration),  # #2
        }
        if i == 0:
            entry["prev_desc"] = None
            entry["prev_beat"] = None
        else:
            prev_scene = scenes_by_idx.get(i - 1, {})
            entry["prev_desc"] = prev_scene.get("desc", "")
            entry["prev_beat"] = prev_scene.get("narrative_beat", "")
        shots.append(entry)
    return shots


def run_compiler(
    plan: dict[str, Any],
    concept: dict[str, Any],
    state: dict[str, Any],
    llm_call: LLMCall,
) -> dict[str, Any]:
    """Step 4: compile T2V/I2V prompts. Returns {shot_id: {positive, negative}}."""
    answers = state.get("clarification_answers", {})
    style_tone = answers.get("style_tone", ["fresh"])
    if isinstance(style_tone, str):
        style_tone = [style_tone]

    vs = concept.get("visual_signature", {})
    shots = _build_cross_shot_sequence(plan)

    # #5: pre-translate palette hex codes → descriptive names
    raw_palette = vs.get("color_palette", "#00B894 teal, #FFE082 straw, #FFFFFF white")
    named_palette = _translate_palette(raw_palette)

    user_msg = COMPILER_USER_TEMPLATE.format(
        mood=concept.get("mood", "fresh"),
        visual_style=concept.get("visual_style", "cinematic"),
        style_keywords=_mood_to_style_keywords(concept.get("mood", "fresh")),
        style_tone=", ".join(style_tone),
        vs_camera_style=vs.get("camera_style", "locked-off — no handheld"),
        vs_color_palette_named=named_palette,
        vs_lighting=vs.get("lighting", "soft natural window light"),
        vs_visual_motif=vs.get("visual_motif", "consistent product surface"),
        shots_json=json.dumps(shots, ensure_ascii=False, indent=2),
    )

    try:
        with console.status("[cyan][compiler] Compiling prompts…[/cyan]"):
            raw = llm_call(COMPILER_SYSTEM, user_msg)
        prompts = _parse_json(raw)
        if not isinstance(prompts, dict):
            prompts = {}
        console.print(f"[green][compiler][/green] {len(prompts)} prompt(s) compiled")
        return prompts
    except Exception as e:
        console.print(f"[yellow][compiler] Error: {e} — executor will build prompts[/yellow]")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ══════════════════════════════════════════════════════════════════════════════


def run_creative_pipeline(
    state: dict[str, Any],
    project_id: str,
    llm_call: LLMCall,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Run all 4 steps. Returns (concept, plan, prompts)."""
    # If replanning from feedback, reuse existing concept if available
    existing_concept = state.get("creative_concept")
    feedback = state.get("plan_feedback", "")

    if feedback and existing_concept:
        # Feedback path: skip Director, keep concept
        concept = existing_concept
        console.print("[dim][pipeline] Reusing concept for feedback replan[/dim]")
    else:
        # Fresh plan: run Director
        concept = run_director(state, llm_call)

    plan = run_storyboard(state, concept, project_id, llm_call)

    # If storyboard signals clarification needed, surface it immediately
    if plan.get("clarification_needed"):
        return concept, plan, {}

    plan = run_critic(plan, llm_call)
    prompts = run_compiler(plan, concept, state, llm_call)

    return concept, plan, prompts


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_json(raw: str) -> Any:
    """Extract and parse JSON from a raw LLM response.

    Handles: markdown fences, leading prose, trailing prose/explanation.
    Uses raw_decode so trailing text after the JSON is silently ignored.
    """
    text = raw.strip()
    if not text:
        raise ValueError("Empty response from LLM")
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    if not text:
        raise ValueError("Empty response after stripping fences")

    decoder = json.JSONDecoder()
    # Try from the beginning first
    try:
        value, _ = decoder.raw_decode(text)
        return value
    except json.JSONDecodeError as first_err:
        # If starts with { or [ but failed, likely a truncated response — surface the error clearly
        if text[0] in ("{", "["):
            raise ValueError(
                f"JSON parse failed (likely truncated response — increase max_tokens): {first_err}"
            ) from first_err
    # Scan for first JSON object { — prefer objects over arrays to avoid grabbing inner arrays
    for start_char in ("{", "["):
        idx = text.find(start_char)
        if idx >= 0:
            try:
                value, _ = decoder.raw_decode(text, idx)
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError(f"No valid JSON in response: {text[:80]!r}")
