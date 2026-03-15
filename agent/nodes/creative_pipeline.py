"""creative_pipeline — Director → Storyboard → Critic → PromptCompiler.

Four focused LLM calls replace the monolithic planner_llm prompt.
Each step has a deterministic mock fallback so the pipeline runs without an API key.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from typing import Any, Callable

from rich.console import Console

console = Console()
logger = logging.getLogger(__name__)

# ── Type alias ────────────────────────────────────────────────────────────────

LLMCall = Callable[[str, str], str]  # (system_prompt, user_prompt) -> raw text


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Director
# ══════════════════════════════════════════════════════════════════════════════

DIRECTOR_SYSTEM = """\
You are a creative director for viral short-form social media videos (TikTok / Reels / Shorts).
Given a brief and brand context, generate 3 DISTINCT creative concepts, then select the best one.

Output ONLY valid JSON (no markdown fences):
{
  "concepts": [
    {
      "id": "C1",
      "hook_angle": "<hook strategy — see archetypes below>",
      "visual_style": "<cinematography style, e.g. 'POV handheld + ASMR macro inserts'>",
      "key_message": "<core message in 1 sentence>",
      "mood": "<emotional tone, e.g. 'energetic', 'serene', 'playful', 'luxurious'>",
      "scene_count": <int 4-7>,
      "visual_signature": {
        "camera_style": "<single unified camera movement for ALL shots, e.g. 'handheld drift with micro-shake — warm and authentic'>",
        "color_palette": "<2-3 hex codes + descriptive names, e.g. '#00B894 deep teal, #FFE082 warm straw, #FFFFFF clean white'>",
        "lighting": "<unified lighting setup for all shots, e.g. 'golden-hour backlight with warm lens flare'>",
        "visual_motif": "<repeating visual element threading all shots, e.g. 'condensation droplets on cup surface'>"
      }
    },
    { ... concept 2 ... },
    { ... concept 3 ... }
  ],
  "best_index": <0-based index of the concept that best fits this brief and brand>
}

━━ MODERN HOOK ARCHETYPES — choose one per concept, all 3 must differ ━━
- "pov-immersion"     : viewer IS the protagonist — hand reaches into frame, eyes pan toward product,
                        first-person perspective makes viewer feel they are experiencing the moment
- "problem-contrast"  : open on a relatable pain point (sweating in heat, exhausted, stressed),
                        then cut to product as instant relief — contrast drives emotional payoff
- "asmr-reveal"       : extreme sensory close-ups FIRST (ice cube dropping, liquid slow-pour, condensation
                        forming on glass, steam rising) — product identity withheld until midpoint
- "micro-story"       : complete 5-8 second narrative arc — stranger encounters product → curiosity →
                        first sip → visible transformation (smile, relief, energy)
- "social-proof"      : feels unscripted and authentic — friend films friend candid reaction,
                        casual handheld, imperfect framing, genuine emotion

━━ HOOK RULES (MANDATORY) ━━
- First 1.5 seconds MUST create intrigue, tension, or sensory pull — NEVER open with a product shot
- NEVER start with: product close-up, logo, brand name, generic outdoor establishing shot
- START with: a relatable human emotion, a striking sensory detail, a surprising contrast, a question implied by action
- Ending scene should be loop-friendly: final motion echoes or leads back into the first frame's energy

━━ VISUAL STYLE OPTIONS (pick cinematography that matches the hook) ━━
Allowed: macro, ASMR-close-up, POV handheld, lifestyle candid, overhead flat-lay, slow-motion,
push-in dolly, crane float, locked-off symmetry — anything a real camera captures in one take.

━━ T2V GENERABILITY — FORBIDDEN ━━
NEVER use: split-screen, color gel, halftone, collision dissolve, wipe transitions, digital zoom,
After Effects, motion graphics, animated text, kinetic typography, compositing two video layers.

━━ CONCEPT DIFFERENTIATION ━━
- Each concept must differ in: hook_angle, visual_style, mood, AND cinematography approach
- best_index: choose the concept with highest viral potential for the platform + brand fit
"""

DIRECTOR_USER_TEMPLATE = """\
Brief: {brief}
Brand: {brand_name}, primary color: {primary_color}, CTA: "{outro_cta}"
Platform: {platform}, Duration: {duration_sec}s, Language: {language}

Generate 3 creative concepts and select the best one. Infer the appropriate tone and mood from the brief.
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
    )

    # Append auto-fix addendum if present (from system_config planner_prompt_addendum)
    addendum = state.get("_planner_addendum", "")
    effective_director_system = DIRECTOR_SYSTEM + (
        f"\n\nADDITIONAL RULES (from user feedback analysis):\n{addendum}" if addendum else ""
    )

    try:
        t0 = time.time()
        with console.status("[cyan][director] Generating creative concepts…[/cyan]"):
            raw = llm_call(effective_director_system, user_msg)
        logger.info("[director] llm_call: %.1fs", time.time() - t0)
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
You are a viral short-form video director and storyboard artist (TikTok / Reels / Shorts).
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
    "hook": "<opening line — creates intrigue or asks an implicit question, 5-10 words>",
    "body": ["<line1>", "<line2>", "<line3>"],
    "cta": "<call to action, 5-10 words>"
  },
  "storyboard": [
    {
      "scene": 1,
      "desc": "<ACTIVE visual description — what is HAPPENING, not what is SHOWING. Use strong action verbs.>",
      "duration": <float>,
      "asset_hint": "<macro|lifestyle|text|product|pov|asmr>",
      "narrative_beat": "<hook|tension|contrast|tease|build|reveal|climax|payoff>",
      "transition_in": "<specific cut technique: 'smash cut from S1 heat haze', 'match on action — hand motion continues', 'whip-pan right into product', 'jump cut — same subject, tighter frame'>"
    },
    ...
  ],
  "shot_list": [
    {"shot_id": "S1", "type": "<macro|wide|close|text|transition|pov|asmr>", "asset": "<asset key or 'generate'>", "text_overlay": "<text>", "duration": <float>},
    ...
  ],
  "render_targets": ["9:16"]
}

━━ SCENE CONSTRUCTION RULES ━━
- scene_count from concept is a guide; add scenes if needed for narrative flow
- Each shot duration: 0.5–2.0 seconds (shorter = more energy; use 1.5-2.0 for emotional moments)
- shot_list must 1-to-1 match storyboard scenes (S1, S2, … SN)
- duration_sec = sum of all shot durations
- narrative_beat arc: hook → tension/contrast → tease/build → reveal → climax → payoff
- Every desc MUST use color_palette colors and camera_style from visual_signature

━━ HOOK RULES (Scene 1 — MANDATORY) ━━
- Scene 1 MUST NOT show the product. Open with: human emotion, problem, sensory detail, or surprising action
- Hook must create immediate intrigue — viewer asks "what happens next?" within 1.5s
- Examples by hook_angle:
  • pov-immersion  → viewer's hand reaches into frame, sweat on skin, blurred heat haze in background
  • problem-contrast → person fanning themselves, close-up of sweat on neck, tired expression
  • asmr-reveal    → extreme close-up: ice cube dropped into empty glass, water droplets form in slow-mo
  • micro-story    → stranger walking past street stall, eyes catch color of the drink, feet stop
  • social-proof   → phone held sideways filming a friend's candid reaction to first sip

━━ NEW SHOT TYPES ━━
- "pov"  : first-person perspective — camera = viewer's eyes, hands visible, immersive angle
- "asmr" : extreme sensory macro — ice dropping, liquid pouring, condensation forming, texture reveal.
           Focus is TACTILE and AUDITORY (describe what would make sound/feel satisfying)

━━ CONTRAST & TENSION (for food/beverage/lifestyle) ━━
- Use narrative_beat "tension" or "contrast" for a before/after moment
- Pair hot/dry/exhausted with cold/refreshing/energized in adjacent scenes
- Smash cuts and jump cuts between "problem" and "solution" scenes create viral energy

━━ TRANSITION LANGUAGE (be specific, not generic) ━━
GOOD: "smash cut — heat shimmer fades into ice-cold condensation", "match on action — hand motion from S2 continues picking up cup", "whip-pan right into product reveal"
BAD:  "smooth transition", "cut to next scene", "transition from previous"

━━ DESC WRITING RULES (MANDATORY) ━━
- Use strong ACTION VERBS: "hand reaches", "ice cube drops", "condensation crawls", "lips part", "eyes widen"
- Describe MOTION first, then framing, then lighting — never a static tableau
- Include sensory specificity: "tiny bubbles race up the side of the glass", "steam curls off the surface"
- NEVER mention: text, captions, logos, watermarks, overlays, taglines, "branded", "branding"
- NEVER use: "shows", "displays", "features", "presents" — these are static; use dynamic verbs
- Text content → ONLY in text_overlay fields

━━ OUTRO ━━
- Type "text" shot is optional — include ONLY if user explicitly requests a brand card
- If included, desc = abstract visual background only (bokeh, color wash, mood)

━━ FEEDBACK / MODIFICATION RULES ━━
- Follow modification requests EXACTLY and LITERALLY
- "modify scene N" / "change scene N to X" → edit in place
- "add scene between N and N+1" → INSERT, renumber
- "remove scene N" → DELETE, renumber
- "add X to scene N" → update desc, do NOT insert new scene
- AMBIGUOUS request → {"clarification_needed": true, "question": "<specific question>"}
"""

STORYBOARD_USER_TEMPLATE = """\
Brief: {brief}
Brand: {brand_name}, primary color: {primary_color}, CTA: "{outro_cta}"
Platform: {platform}, Duration: {duration_sec}s, Language: {language}

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
        t0 = time.time()
        with console.status("[cyan][storyboard] Generating storyboard…[/cyan]"):
            raw = llm_call(STORYBOARD_SYSTEM, user_msg)
        logger.info("[storyboard] llm_call: %.1fs", time.time() - t0)
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
            answers_copy.get("style_tone", ["fresh"]),
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

    # A1: VFX pattern violations
    violations: list[int] = []
    for i, scene in enumerate(storyboard):
        if _VFX_PATTERNS.search(scene.get("desc", "")):
            violations.append(i)

    # A2: shot_type / desc mismatch — "product" (I2V) used for complex multi-object scenes
    # I2V only animates the product image; it cannot render backgrounds, props, or environments.
    # If a "product" shot's desc mentions surrounding objects or a specific background,
    # auto-correct the type to "lifestyle" so T2V renders the full scene.
    _COMPLEX_SCENE_PATTERNS = re.compile(
        r"surround(?:ed|ing)|arranged\s+(?:symmetrically|around)|flat[\s-]lay"
        r"|background\s+(?:of|with|shows?)|teal\s+surface|(?:coconut|watermelon|ingredient)"
        r"\s+(?:wedge|slice|shell|piece)|overhead\s+(?:shot|crop|flat)"
        r"|side[\s-]by[\s-]side|next\s+to\s+(?:it|the\s+cup|the\s+product)",
        re.IGNORECASE,
    )
    type_fixes: list[int] = []
    for i, (scene, shot) in enumerate(zip(storyboard, shot_list)):
        if shot.get("type") == "product" and _COMPLEX_SCENE_PATTERNS.search(scene.get("desc", "")):
            type_fixes.append(i)

    if type_fixes:
        console.print(
            f"[yellow][critic][/yellow] {len(type_fixes)} shot_type mismatch(es) — "
            "product→lifestyle (complex scene needs T2V)"
        )
        for i in type_fixes:
            shot_list[i]["type"] = "lifestyle"
            storyboard[i]["asset_hint"] = "lifestyle"
            console.print(f"  [green]S{storyboard[i]['scene']}[/green] type: product → lifestyle")

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
        t0 = time.time()
        with console.status("[cyan][critic] QC check…[/cyan]"):
            raw = llm_call(CRITIC_SYSTEM, user_msg)
        logger.info("[critic] qc_check: %.1fs", time.time() - t0)
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
    vs = concept.get("visual_signature", {})
    shots = _build_cross_shot_sequence(plan)

    # #5: pre-translate palette hex codes → descriptive names
    raw_palette = vs.get("color_palette", "#00B894 teal, #FFE082 straw, #FFFFFF white")
    named_palette = _translate_palette(raw_palette)

    user_msg = COMPILER_USER_TEMPLATE.format(
        mood=concept.get("mood", "fresh"),
        visual_style=concept.get("visual_style", "cinematic"),
        style_keywords=_mood_to_style_keywords(concept.get("mood", "fresh")),
        vs_camera_style=vs.get("camera_style", "locked-off — no handheld"),
        vs_color_palette_named=named_palette,
        vs_lighting=vs.get("lighting", "soft natural window light"),
        vs_visual_motif=vs.get("visual_motif", "consistent product surface"),
        shots_json=json.dumps(shots, ensure_ascii=False, indent=2),
    )

    try:
        t0 = time.time()
        with console.status("[cyan][compiler] Compiling prompts…[/cyan]"):
            raw = llm_call(COMPILER_SYSTEM, user_msg)
        logger.info("[compiler] llm_call: %.1fs", time.time() - t0)
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
    t_pipeline = time.time()

    # If replanning from feedback, reuse existing concept if available
    existing_concept = state.get("creative_concept")
    feedback = state.get("plan_feedback", "")

    if feedback and existing_concept:
        # Feedback path: skip Director, keep concept
        concept = existing_concept
        console.print("[dim][pipeline] Reusing concept for feedback replan[/dim]")
    else:
        # Fresh plan: run Director
        t1 = time.time()
        concept = run_director(state, llm_call)
        logger.info("[pipeline] director: %.1fs", time.time() - t1)

    t2 = time.time()
    plan = run_storyboard(state, concept, project_id, llm_call)
    logger.info("[pipeline] storyboard: %.1fs", time.time() - t2)

    # If storyboard signals clarification needed, surface it immediately
    if plan.get("clarification_needed"):
        return concept, plan, {}

    t3 = time.time()
    plan = run_critic(plan, llm_call)
    logger.info("[pipeline] critic: %.1fs", time.time() - t3)

    t4 = time.time()
    prompts = run_compiler(plan, concept, state, llm_call)
    logger.info("[pipeline] compiler: %.1fs", time.time() - t4)

    logger.info("[pipeline] total: %.1fs", time.time() - t_pipeline)
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
