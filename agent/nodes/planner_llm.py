"""planner_llm — orchestrate creative pipeline (Director→Storyboard→Critic→Compiler)."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from rich.console import Console

console = Console()

# ── Keep PLANNER_SYSTEM for backward compat (used by tests) ───────────────────

PLANNER_SYSTEM = """You are an expert short-form video script writer and director.
Given a brief, brand kit, and user preferences, produce a complete video plan as valid JSON.

Output ONLY a JSON object matching this schema (no markdown fences):
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
    {"scene": 1, "desc": "<visual description>", "duration": <float>, "asset_hint": "<macro|lifestyle|product|pov|asmr>"},
    ...
  ],
  "shot_list": [
    {"shot_id": "S1", "type": "<macro|wide|close|pov|asmr>", "asset": "<asset key or 'generate'>", "duration": <float>},
    ...
  ],
  "render_targets": ["9:16"]
}

Rules:
- Default to 4 scenes/shots for a typical short video; adjust count based on user requirements
- Each shot duration must be between 0.5 and 2.0 seconds
- shot_list must have one entry per storyboard scene (S1, S2, … SN)
- Hook is always scene 1; last scene is typically a CTA or outro unless user specifies otherwise
- Match brand tone and style
- duration_sec should equal the sum of all shot durations

CRITICAL — feedback / modification rules:
- Follow the user's modification request EXACTLY and LITERALLY
- If the user says "modify scene N" or "change scene N to X": edit that scene's desc/type in place
- If the user says "add a scene between scene N and scene N+1": INSERT a new scene, renumber subsequent scenes, total count increases by 1
- If the user says "remove scene N": DELETE that scene, renumber subsequent scenes, total count decreases by 1
- If the user says "add X to scene N": update scene N's desc to include X — do NOT insert a new scene
- The scene count CHANGES whenever the user explicitly adds or removes scenes; otherwise keep it the same
- If the modification request is AMBIGUOUS or UNCLEAR, do NOT guess — output a JSON with a special field:
  {"clarification_needed": true, "question": "<specific question to ask the user>"}

CRITICAL — storyboard desc rules:
- "desc" fields describe ONLY visual scene elements: motion, lighting, composition, colors, environment, camera angle, textures
- NEVER mention text, captions, logos, watermarks, overlays, written words, taglines, slogans, or any on-screen graphics in desc
- NEVER use words like: "text appears", "logo shown", "caption", "title card", "CTA", "branded graphic", "overlay", "tagline", "branded cup", "branded bottle", "branding"
- NEVER use the word "branded" — say "product cup", "product bottle", "the drink" instead
- Do NOT include any text overlay shots or brand cards — every shot must be a real visual scene
- shot_list must correspond 1-to-1 with storyboard scenes — no extra shots, no missing shots
"""


def _get_prompt_addendum() -> str:
    """Read planner_prompt_addendum from system_config (auto-fix loop)."""
    try:
        import json as _json
        import agent.deps as _deps
        val = _deps.db().get_system_config("planner_prompt_addendum")
        return _json.loads(val) if val else ""
    except Exception:
        return ""


def planner_llm(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state.get("project_id", str(uuid.uuid4())[:8])

    # Build LLM call function
    llm_call = _build_llm_call()

    # Get Gemini client for interleaved image generation (may be None)
    gemini_client = get_gemini_client()

    # Inject auto-fix addendum into state so creative_pipeline can append it
    addendum = _get_prompt_addendum()
    if addendum:
        state = {**state, "_planner_addendum": addendum}

    # Import pipeline
    from agent.nodes.creative_pipeline import run_creative_pipeline

    concept, plan_dict, prompts, concept_images = run_creative_pipeline(
        state, project_id, llm_call, gemini_client=gemini_client
    )

    # If storyboard signals clarification needed
    if plan_dict.get("clarification_needed"):
        question = plan_dict.get("question", "Could you clarify your request?")
        messages = state.get("messages", [])
        messages.append({"role": "assistant", "content": question})
        return {
            "messages": messages,
            "needs_replan": False,
            "needs_user_action": True,
        }

    # Ensure plan fields
    plan_dict["project_id"] = project_id
    plan_dict["brief"] = state.get("brief", "")

    # Embed concept images inside plan so they survive DB round-trip
    if concept_images:
        plan_dict["concept_images"] = concept_images

    # Preserve product_info (brand_info, logo_path, etc.) inside plan so it
    # survives the DB round-trip and is available to modify/execute runs later.
    product_info = state.get("product_info")
    if product_info:
        plan_dict["product_info"] = product_info

    plan_version = state.get("plan_version", 0) + 1
    messages = state.get("messages", [])
    img_note = f", {len(concept_images)} concept images" if concept_images else ""
    messages.append({
        "role": "assistant",
        "content": f"[planner_llm] plan v{plan_version} ready, {len(plan_dict.get('shot_list', []))} shots{img_note}",
    })

    result: dict[str, Any] = {
        "plan": plan_dict,
        "plan_version": plan_version,
        "needs_replan": False,
        "messages": messages,
        "creative_concept": concept,
        "concept_images": concept_images,
    }
    if prompts:
        result["t2v_prompts"] = prompts

    return result


# ── LLM backend factory ────────────────────────────────────────────────────────


def _build_llm_call():
    """Return a (system, user) -> str callable using the best available LLM.

    Priority: Gemini (Google) → Anthropic (Claude) → OpenAI → Mock
    """
    api_key_google = os.getenv("GOOGLE_API_KEY")
    api_key_anthropic = os.getenv("ANTHROPIC_API_KEY")
    api_key_openai = os.getenv("OPENAI_API_KEY")

    if api_key_google:
        console.print("[dim][planner] Using Gemini 2.0 Flash[/dim]")
        return _make_gemini_call()
    elif api_key_anthropic:
        return _make_anthropic_call()
    elif api_key_openai:
        return _make_openai_call()
    else:
        console.print("[dim][planner] No API key — using mock planner[/dim]")
        return _mock_llm_call


def get_gemini_client():
    """Return a Gemini genai.Client if GOOGLE_API_KEY is set, else None."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except Exception as e:
        console.print(f"[yellow][planner] Gemini client init failed: {e}[/yellow]")
        return None


def _make_gemini_call():
    try:
        from google import genai
        from google.genai import types

        api_key = os.getenv("GOOGLE_API_KEY")
        client = genai.Client(api_key=api_key)

        def call(system: str, user: str) -> str:
            combined = f"{system}\n\n---\n\n{user}"
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=combined,
                config=types.GenerateContentConfig(
                    max_output_tokens=8192,
                    temperature=0.7,
                ),
            )
            return response.text

        return call
    except Exception as e:
        console.print(f"[yellow][planner] Gemini call init error: {e}[/yellow]")
        return _mock_llm_call


def _make_anthropic_call():
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=8192)  # type: ignore[call-arg]

        def call(system: str, user: str) -> str:
            response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            return response.content if hasattr(response, "content") else str(response)

        return call
    except Exception as e:
        console.print(f"[yellow][planner] Anthropic init error: {e}[/yellow]")
        return _mock_llm_call


def _make_openai_call():
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatOpenAI(model="gpt-4o", max_tokens=8192, response_format={"type": "json_object"})

        def call(system: str, user: str) -> str:
            response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            return response.content if hasattr(response, "content") else str(response)

        return call
    except Exception as e:
        console.print(f"[yellow][planner] OpenAI init error: {e}[/yellow]")
        return _mock_llm_call


def _mock_llm_call(system: str, user: str) -> str:
    """Fallback: returns empty JSON so each step falls through to its own mock."""
    return "{}"


# ── Mock planner (deterministic for Tong Sui demo) ────────────────────────────


def _mock_plan(
    state: dict,
    project_id: str,
    platform: str,
    duration_sec: int,
    language: str,
    style_tone: list[str],
) -> dict:
    """Deterministic plan for Tong Sui Coconut Watermelon Refresh."""
    brief = state.get("brief", "").lower()
    is_tong_sui = "tong sui" in brief or "coconut" in brief or "watermelon" in brief

    brand_kit = state.get("brand_kit", {})
    cta_text = brand_kit.get("intro_outro", {}).get("outro_cta", "Order now")

    if language == "zh":
        hook = "夏日清凉，来一口椰子西瓜！"
        body_lines = ["100% 天然果汁", "清爽不腻，喝一口停不下来", "限定口味，数量有限"]
        cta = "立刻下单，享受夏天"
        shot_texts = ["夏日来一口", "100% 天然", "清爽不腻", "限定口味", "🛒 立刻下单"]
    else:
        hook = "Summer just got cooler 🥥🍉"
        body_lines = [
            "Introducing Coconut Watermelon Refresh",
            "100% natural, zero guilt",
            "The taste of summer in every sip",
        ]
        cta = f"{cta_text} — limited time only"
        shot_texts = [
            "Summer just got cooler 🥥🍉",
            "Coconut Watermelon Refresh",
            "100% Natural",
            "Zero Guilt, Pure Bliss",
            cta_text,
        ]

    scene_descs = [
        ("Macro shot of coconut and watermelon slices with water droplets, vibrant colors, studio lighting", "macro"),
        ("Product bottle center frame, clean vibrant background, slow rotation, cinematic lighting", "product"),
        ("Lifestyle shot, someone holding the drink at a summer poolside, golden hour lighting", "lifestyle"),
    ]
    trim_durations = [1.5, 2.0, 2.0]

    storyboard = []
    shot_list = []
    for i in range(3):
        desc, asset_hint = scene_descs[i]
        dur = trim_durations[i]
        scene_num = i + 1
        storyboard.append({
            "scene": scene_num,
            "desc": desc,
            "duration": dur,
            "asset_hint": asset_hint,
        })
        shot_list.append({
            "shot_id": f"S{scene_num}",
            "type": asset_hint,
            "asset": "generate" if not is_tong_sui else f"tong_sui_{asset_hint}",
            "duration": dur,
        })

    actual_duration = round(sum(trim_durations), 1)
    return {
        "project_id": project_id,
        "platform": platform,
        "duration_sec": actual_duration,
        "language": language,
        "style_tone": style_tone,
        "script": {"hook": hook, "body": body_lines, "cta": cta},
        "storyboard": storyboard,
        "shot_list": shot_list,
        "render_targets": ["9:16"],
    }
