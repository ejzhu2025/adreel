"""planner_llm — produce Plan JSON via LLM or deterministic mock."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from rich.console import Console
from rich.spinner import Spinner

console = Console()

# ── Prompt template ───────────────────────────────────────────────────────────

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
    {"scene": 1, "desc": "<visual description>", "duration": <float>, "asset_hint": "<macro|lifestyle|text|product>"},
    ...
  ],
  "shot_list": [
    {"shot_id": "S1", "type": "<macro|wide|close|text|transition>", "asset": "<asset key or 'generate'>", "text_overlay": "<text>", "duration": <float>},
    ...
  ],
  "render_targets": ["9:16"]
}

Rules:
- Always generate exactly 4 scenes/shots
- Each shot duration must be between 0.5 and 2.0 seconds
- shot_list must have one entry per storyboard scene (S1–S4)
- Hook is always scene 1, CTA always last scene
- Match brand tone and style
- Keep text_overlay short (max 8 words)
- duration_sec should equal the sum of all shot durations
"""

PLANNER_USER_TEMPLATE = """Brief: {brief}
Brand: {brand_name}, primary color: {primary_color}, CTA: "{outro_cta}"
Platform: {platform}, Duration: {duration_sec}s, Language: {language}
Tone: {style_tone}
Feedback (if any): {feedback}
Similar past projects (for reference): {similar_projects}

Generate the plan JSON now."""


def planner_llm(state: dict[str, Any]) -> dict[str, Any]:
    answers = state.get("clarification_answers", {})
    brand_kit = state.get("brand_kit", {})
    project_id = state.get("project_id", str(uuid.uuid4())[:8])
    feedback = state.get("plan_feedback", "")
    similar = state.get("similar_projects", [])

    platform = answers.get("platform", "tiktok")
    duration_sec = int(answers.get("duration_sec", 20))
    language = answers.get("language", "en")
    style_tone = answers.get("style_tone", ["fresh"])
    if isinstance(style_tone, str):
        style_tone = [style_tone]

    similar_str = json.dumps([s.get("document", "") for s in similar[:2]], ensure_ascii=False)

    user_msg = PLANNER_USER_TEMPLATE.format(
        brief=state.get("brief", ""),
        brand_name=brand_kit.get("name", brand_kit.get("brand_id", "Brand")),
        primary_color=brand_kit.get("colors", {}).get("primary", "#00B894"),
        outro_cta=brand_kit.get("intro_outro", {}).get("outro_cta", "Order now"),
        platform=platform,
        duration_sec=duration_sec,
        language=language,
        style_tone=", ".join(style_tone) if isinstance(style_tone, list) else style_tone,
        feedback=feedback or "None",
        similar_projects=similar_str,
    )

    plan_dict: dict[str, Any] | None = None

    # Try LLM first
    api_key_anthropic = os.getenv("ANTHROPIC_API_KEY")
    api_key_openai = os.getenv("OPENAI_API_KEY")

    if api_key_anthropic:
        plan_dict = _call_anthropic(user_msg, project_id)
    elif api_key_openai:
        plan_dict = _call_openai(user_msg, project_id)
    else:
        # Try Claude via claude-sonnet-4-6 even without explicit API key
        # (works when ANTHROPIC_API_KEY is set in env or .env)
        pass

    # Fallback to mock
    if plan_dict is None:
        console.print("[dim][planner] No API key found — using mock planner[/dim]")
        plan_dict = _mock_plan(state, project_id, platform, duration_sec, language, style_tone)

    # Ensure project_id matches
    plan_dict["project_id"] = project_id
    plan_dict["brief"] = state.get("brief", "")

    plan_version = state.get("plan_version", 0) + 1

    messages = state.get("messages", [])
    messages.append({"role": "assistant", "content": f"[planner_llm] plan v{plan_version} ready, {len(plan_dict.get('shot_list', []))} shots"})

    return {
        "plan": plan_dict,
        "plan_version": plan_version,
        "needs_replan": False,
        "messages": messages,
    }


# ── LLM backends ──────────────────────────────────────────────────────────────

def _call_anthropic(user_msg: str, project_id: str) -> dict | None:
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=2048)  # type: ignore[call-arg]
        with console.status("[cyan]Calling Claude to generate plan…[/cyan]"):
            response = llm.invoke([SystemMessage(content=PLANNER_SYSTEM), HumanMessage(content=user_msg)])
        raw = response.content if hasattr(response, "content") else str(response)
        return json.loads(raw)
    except Exception as e:
        console.print(f"[yellow][planner] Anthropic error: {e}[/yellow]")
        return None


def _call_openai(user_msg: str, project_id: str) -> dict | None:
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatOpenAI(model="gpt-4o", max_tokens=2048, response_format={"type": "json_object"})
        with console.status("[cyan]Calling GPT-4o to generate plan…[/cyan]"):
            response = llm.invoke([SystemMessage(content=PLANNER_SYSTEM), HumanMessage(content=user_msg)])
        raw = response.content if hasattr(response, "content") else str(response)
        return json.loads(raw)
    except Exception as e:
        console.print(f"[yellow][planner] OpenAI error: {e}[/yellow]")
        return None


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

    # 4 shots, trim durations 0.5–2s each
    scene_descs = [
        ("Macro shot of coconut and watermelon slices with water droplets", "macro"),
        ("Product bottle center frame, vibrant background", "product"),
        ("Lifestyle: someone holding the drink at a summer pool", "lifestyle"),
        ("Outro: CTA with logo and product", "product"),
    ]
    trim_durations = [1.5, 1.0, 2.0, 1.0]  # total 5.5s

    storyboard = []
    shot_list = []
    for i in range(4):
        desc, asset_hint = scene_descs[i]
        dur = trim_durations[i]
        scene_num = i + 1
        storyboard.append({
            "scene": scene_num,
            "desc": desc,
            "duration": dur,
            "asset_hint": asset_hint,
        })
        overlay_text = shot_texts[i] if i < len(shot_texts) else ""
        shot_list.append({
            "shot_id": f"S{scene_num}",
            "type": asset_hint,
            "asset": "generate" if not is_tong_sui else f"tong_sui_{asset_hint}",
            "text_overlay": overlay_text,
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
