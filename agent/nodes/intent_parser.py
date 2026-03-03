"""intent_parser — extract platform, duration, tone hints from the raw brief."""
from __future__ import annotations

import re
from typing import Any


_PLATFORM_KEYWORDS = {
    "tiktok": ["tiktok", "tik tok"],
    "reels": ["reels", "instagram", "ig"],
    "shorts": ["shorts", "youtube", "yt"],
}

_DURATION_RE = re.compile(r"(\d+)\s*s(?:ec(?:ond)?s?)?", re.IGNORECASE)

_TONE_KEYWORDS = {
    "fresh": ["fresh", "summer", "cool", "light", "refreshing"],
    "premium": ["premium", "luxury", "high-end", "artisan"],
    "playful": ["playful", "fun", "quirky", "cute"],
    "funny": ["funny", "humor", "joke", "meme"],
    "strong_promo": ["promo", "sale", "discount", "offer", "deal"],
}


def intent_parser(state: dict[str, Any]) -> dict[str, Any]:
    brief = state.get("brief", "")
    lower = brief.lower()

    # Extract platform hint
    detected_platform = None
    for platform, keywords in _PLATFORM_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            detected_platform = platform
            break

    # Extract duration hint
    duration_match = _DURATION_RE.search(lower)
    detected_duration = int(duration_match.group(1)) if duration_match else None

    # Extract tone hints
    detected_tones: list[str] = []
    for tone, keywords in _TONE_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            detected_tones.append(tone)

    # Detect language hint
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", brief))
    detected_language = "zh" if has_chinese else "en"

    # Build partial answers from the brief (pre-fill clarification)
    pre_answers: dict[str, Any] = {}
    if detected_platform:
        pre_answers["platform"] = detected_platform
    if detected_duration:
        pre_answers["duration_sec"] = detected_duration
    if detected_tones:
        pre_answers["style_tone"] = detected_tones
    pre_answers["language"] = detected_language

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[intent_parser] brief='{brief[:80]}' detected={pre_answers}",
        }
    )

    return {
        "clarification_answers": {**state.get("clarification_answers", {}), **pre_answers},
        "messages": messages,
    }
