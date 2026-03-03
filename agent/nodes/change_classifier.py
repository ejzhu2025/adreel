"""change_classifier — LLM decides if feedback is a global replan or a local shot fix."""
from __future__ import annotations

import json
import os
from typing import Any

from rich.console import Console

console = Console()

CLASSIFIER_SYSTEM = """You are a video editing assistant. A user generated a short video and wants to modify it.

Classify the modification as:
- "global": requires full replanning (style/tone/mood change, script change, narrative restructure, changing more than half the shots, overall color scheme)
- "local": only 1-3 specific shots need to be regenerated (swap a character, change an object, fix text in one scene, change one scene's visual without affecting others)

For "local" changes, identify the exact shot indices (0-based) and provide updated descriptions.

Output ONLY valid JSON (no markdown fences):
{
  "change_type": "global" | "local",
  "reasoning": "<one sentence>",
  "affected_shot_indices": [0, 2],
  "shot_updates": {
    "0": {
      "desc": "<new full scene visual description>",
      "text_overlay": "<new overlay text, keep ≤8 words>"
    }
  }
}
For "global", set affected_shot_indices=[] and shot_updates={}.
"""

CLASSIFIER_USER = """Current video plan:
{plan_summary}

User modification request: "{feedback}"

Classify and output JSON now."""


def change_classifier(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    feedback = state.get("plan_feedback", "")

    # Build compact plan summary
    storyboard = plan.get("storyboard", [])
    shot_list = plan.get("shot_list", [])
    lines = [
        f"Platform: {plan.get('platform','tiktok')}  Duration: {plan.get('duration_sec','?')}s",
        f"Style: {', '.join(plan.get('style_tone', []))}",
        f"Hook: \"{plan.get('script', {}).get('hook', '')}\"",
        "",
        "Shots:",
    ]
    for i, (scene, shot) in enumerate(zip(storyboard, shot_list)):
        overlay = f' | overlay: "{shot.get("text_overlay","")}"' if shot.get("text_overlay") else ""
        lines.append(f"  [{i}] {shot.get('shot_id',f'S{i+1}')} — {scene.get('desc','')[:80]}{overlay}")
    plan_summary = "\n".join(lines)

    user_msg = CLASSIFIER_USER.format(plan_summary=plan_summary, feedback=feedback)

    result = None
    if os.getenv("ANTHROPIC_API_KEY"):
        result = _call_anthropic(user_msg)

    if result is None:
        # Heuristic fallback
        global_kw = ["style", "tone", "mood", "script", "entire", "whole", "all shots",
                     "completely", "redo", "color scheme", "narrative", "structure"]
        is_global = any(k in feedback.lower() for k in global_kw)
        result = {
            "change_type": "global" if is_global else "local",
            "reasoning": "Heuristic fallback (no API key)",
            "affected_shot_indices": [] if is_global else list(range(len(storyboard))),
            "shot_updates": {},
        }

    change_type = result.get("change_type", "global")
    affected = [int(i) for i in result.get("affected_shot_indices", [])]
    shot_updates = {str(k): v for k, v in result.get("shot_updates", {}).items()}

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": (
            f"[change_classifier] type={change_type}, "
            f"affected={affected}, reason={result.get('reasoning','')}"
        ),
    })

    return {
        "change_type": change_type,
        "affected_shot_indices": affected,
        "shot_updates": shot_updates,
        "messages": messages,
    }


def _call_anthropic(user_msg: str) -> dict | None:
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        # Use haiku for classification — fast and cheap
        llm = ChatAnthropic(model="claude-haiku-4-5-20251001", max_tokens=512)  # type: ignore[call-arg]
        with console.status("[cyan]Classifying modification scope…[/cyan]"):
            response = llm.invoke([
                SystemMessage(content=CLASSIFIER_SYSTEM),
                HumanMessage(content=user_msg),
            ])
        raw = response.content if hasattr(response, "content") else str(response)
        raw = raw.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        console.print(f"[yellow][change_classifier] error: {e}[/yellow]")
        return None
