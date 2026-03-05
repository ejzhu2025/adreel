"""narrative_coherence — LLM judge via Claude claude-sonnet-4-6."""
from __future__ import annotations

import json
import os
from typing import Any


def score(brief: str, storyboard: list[dict]) -> dict[str, Any]:
    """
    Ask Claude to score the storyboard 1–5 for narrative logic.
    Normalizes to 0–1 via (score - 1) / 4.
    Skips gracefully if ANTHROPIC_API_KEY not set.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"score": None, "rationale": "No ANTHROPIC_API_KEY", "skipped": True}

    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        storyboard_str = json.dumps(storyboard, ensure_ascii=False, separators=(",", ":"))

        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=api_key,
            max_tokens=256,
            temperature=0,
        )

        messages = [
            SystemMessage(
                content=(
                    'You are a video editor. Score this shot sequence 1–5 for narrative logic: '
                    'hook→buildup→climax→CTA. Output only JSON: {"score": int, "rationale": str}'
                )
            ),
            HumanMessage(
                content=f"Brief: {brief}\n\nStoryboard: {storyboard_str}"
            ),
        ]

        response = llm.invoke(messages)
        text = response.content.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        parsed = json.loads(text)
        raw_score = int(parsed.get("score", 3))
        raw_score = max(1, min(5, raw_score))
        normalized = (raw_score - 1) / 4

        return {
            "score": round(normalized, 4),
            "raw_score": raw_score,
            "rationale": parsed.get("rationale", ""),
            "skipped": False,
        }

    except Exception as e:
        return {
            "score": None,
            "rationale": f"Error: {e}",
            "skipped": True,
        }
