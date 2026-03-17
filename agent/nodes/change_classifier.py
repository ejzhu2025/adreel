"""change_classifier — LLM decides if feedback is a global replan or a local shot fix."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

CLASSIFIER_SYSTEM = """You are a video editing assistant. A user generated a short video and wants to modify it.

Classify the modification as:
- "global": requires full replanning — style/tone/mood change, complete script rewrite, narrative restructure, changing more than half the shots, overall color scheme overhaul
- "local": re-render 1-3 existing shots (swap a character, change an object, fix one scene's visual, add/change branding)
- "add_scene": user wants to INSERT a new scene somewhere in the video (beginning, middle, or end)
- "remove_scene": user wants to DELETE one or more specific scenes

For "local":
  - Set affected_shot_indices to the 0-based indices of shots to re-render.
  - Set shot_updates to a dict keyed by index string, each value having a "desc" field with a NEW,
    COMPLETE visual description that incorporates the user's request.
  - IMPORTANT: always provide a new "desc" for every affected shot — the desc is what drives the
    re-render. If the user says a scene looks wrong, write a desc that explicitly avoids the problem
    (e.g. if user says "not crying", write "determined expression, fierce eyes, no tears").
    If the user wants a logo added, write "... with [brand] logo visible in top-right corner".

For "add_scene", provide new_shots: a list of scenes to add, each with position ("first"|"last"|"after:<index>") and a visual desc.
For "remove_scene", provide remove_indices: list of 0-based shot indices to delete.
For "global", set affected_shot_indices=[], shot_updates={}.

Output ONLY valid JSON (no markdown fences):
{
  "change_type": "global" | "local" | "add_scene" | "remove_scene",
  "reasoning": "<one sentence>",
  "affected_shot_indices": [0, 5],
  "shot_updates": {
    "0": {"desc": "<full new visual description for shot 0 incorporating user feedback>"},
    "5": {"desc": "<full new visual description for shot 5 incorporating user feedback>"}
  },
  "new_shots": [
    {"position": "last", "desc": "<full visual scene description>", "type": "lifestyle", "duration": 2.0}
  ],
  "remove_indices": []
}
Omit fields that are not relevant to the change_type.
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
        lines.append(f"  [{i}] {shot.get('shot_id',f'S{i+1}')} — {scene.get('desc','')[:80]}")
    plan_summary = "\n".join(lines)

    user_msg = CLASSIFIER_USER.format(plan_summary=plan_summary, feedback=feedback)

    result = None
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        result = _call_llm(user_msg)

    if result is None:
        # Heuristic fallback
        fb = feedback.lower()
        add_kw = ["add", "insert", "append", "extra scene", "one more scene", "另一个", "加一个", "增加", "加个"]
        remove_kw = ["remove", "delete", "drop scene", "删掉", "删除", "去掉"]
        global_kw = ["style", "tone", "mood", "script", "entire", "whole", "all shots",
                     "completely", "redo", "color scheme", "narrative", "structure"]
        if any(k in fb for k in add_kw):
            result = {
                "change_type": "add_scene",
                "reasoning": "Heuristic: user wants to add a scene",
                "affected_shot_indices": [], "shot_updates": {},
                "new_shots": [{"position": "last", "desc": feedback, "type": "lifestyle", "duration": 2.0}],
                "remove_indices": [],
            }
        elif any(k in fb for k in remove_kw):
            result = {
                "change_type": "remove_scene",
                "reasoning": "Heuristic: user wants to remove a scene",
                "affected_shot_indices": [], "shot_updates": {},
                "new_shots": [], "remove_indices": [],
            }
        else:
            is_global = any(k in fb for k in global_kw)
            result = {
                "change_type": "global" if is_global else "local",
                "reasoning": "Heuristic fallback",
                "affected_shot_indices": [] if is_global else list(range(len(storyboard))),
                "shot_updates": {}, "new_shots": [], "remove_indices": [],
            }

    change_type = result.get("change_type", "global")
    affected = [int(i) for i in result.get("affected_shot_indices", [])]
    shot_updates = {str(k): v for k, v in result.get("shot_updates", {}).items()}
    new_shots = result.get("new_shots", [])
    remove_indices = [int(i) for i in result.get("remove_indices", [])]

    print(
        f"[change_classifier] type={change_type}, affected={affected}, "
        f"shot_updates={list(shot_updates.keys())}, new_shots={len(new_shots)}, "
        f"remove={remove_indices}, reason={result.get('reasoning','')}",
        file=sys.stderr,
    )

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": (
            f"[change_classifier] type={change_type}, "
            f"affected={affected}, new_shots={len(new_shots)}, "
            f"remove={remove_indices}, reason={result.get('reasoning','')}"
        ),
    })

    return {
        "change_type": change_type,
        "affected_shot_indices": affected,
        "shot_updates": shot_updates,
        "new_shots": new_shots,
        "remove_indices": remove_indices,
        "messages": messages,
    }


def _call_llm(user_msg: str) -> dict | None:
    # Prefer Gemini (already used for planning); fall back to Anthropic Haiku
    google_key = os.getenv("GOOGLE_API_KEY")
    if google_key:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=google_key)
            combined = f"{CLASSIFIER_SYSTEM}\n\n---\n\n{user_msg}"
            print(f"[change_classifier] calling Gemini, feedback={user_msg[-120:]!r}", file=sys.stderr)
            resp = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=combined,
                    config=types.GenerateContentConfig(max_output_tokens=512, temperature=0.2),
                )
            raw = resp.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            print(f"[change_classifier] Gemini error: {e}", file=sys.stderr)

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        try:
            from langchain_anthropic import ChatAnthropic
            from langchain_core.messages import HumanMessage, SystemMessage
            llm = ChatAnthropic(model="claude-haiku-4-5-20251001", max_tokens=512)  # type: ignore[call-arg]
            print(f"[change_classifier] calling Anthropic, feedback={user_msg[-120:]!r}", file=sys.stderr)
            response = llm.invoke([
                    SystemMessage(content=CLASSIFIER_SYSTEM),
                    HumanMessage(content=user_msg),
                ])
            raw = response.content if hasattr(response, "content") else str(response)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            print(f"[change_classifier] Anthropic error: {e}", file=sys.stderr)

    return None
