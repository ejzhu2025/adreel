"""clarification_planner — decide which fields still need user input."""
from __future__ import annotations

from typing import Any


# Required fields and their question configs
_REQUIRED_FIELDS = [
    {
        "field": "platform",
        "question": "Which platform is this video for?",
        "options": [
            {"value": "tiktok",  "label": "TikTok (9:16, 15-60s)"},
            {"value": "reels",   "label": "Instagram Reels (9:16, 15-90s)"},
            {"value": "shorts",  "label": "YouTube Shorts (9:16, 60s max)"},
        ],
    },
    {
        "field": "duration_sec",
        "question": "How long should the video be?",
        "options": [
            {"value": 15, "label": "15 seconds — punchy"},
            {"value": 20, "label": "20 seconds — balanced"},
            {"value": 30, "label": "30 seconds — storytelling"},
        ],
    },
    {
        "field": "style_tone",
        "question": "What style / tone should the video have?",
        "options": [
            {"value": ["fresh", "playful"],  "label": "Fresh & Playful"},
            {"value": ["premium"],           "label": "Premium & Minimal"},
            {"value": ["funny"],             "label": "Funny / Meme-style"},
            {"value": ["strong_promo"],      "label": "Strong Promo / Sale"},
        ],
    },
    {
        "field": "language",
        "question": "Caption language?",
        "options": [
            {"value": "en", "label": "English"},
            {"value": "zh", "label": "Chinese (中文)"},
        ],
    },
    {
        "field": "assets_available",
        "question": "Do you have brand assets ready?",
        "options": [
            {"value": "logo_and_product", "label": "Yes — logo + product image"},
            {"value": "logo_only",        "label": "Logo only"},
            {"value": "none",             "label": "None — use generated placeholders"},
        ],
    },
]


def clarification_planner(state: dict[str, Any]) -> dict[str, Any]:
    answers = state.get("clarification_answers", {})

    # Check which fields are missing
    missing_questions = []
    for field_cfg in _REQUIRED_FIELDS:
        field = field_cfg["field"]
        if field not in answers:
            missing_questions.append(field_cfg)

    # Also accept user_prefs defaults as pre-answers
    user_prefs = state.get("user_prefs", {})
    for field_cfg in list(missing_questions):
        field = field_cfg["field"]
        pref_map = {
            "platform": "default_platform",
            "duration_sec": "preferred_duration_sec",
            "style_tone": "tone",
        }
        if field in pref_map and pref_map[field] in user_prefs:
            answers[field] = user_prefs[pref_map[field]]
            missing_questions.remove(field_cfg)

    needs_clarification = len(missing_questions) > 0

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[clarification_planner] needs_clarification={needs_clarification} missing={[q['field'] for q in missing_questions]}",
        }
    )

    return {
        "clarification_needed": needs_clarification,
        "clarification_questions": missing_questions,
        "clarification_answers": answers,
        "messages": messages,
    }
