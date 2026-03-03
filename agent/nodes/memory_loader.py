"""memory_loader — fetch brand kit, user prefs, and similar past projects."""
from __future__ import annotations

from typing import Any

import agent.deps as deps


def memory_loader(state: dict[str, Any]) -> dict[str, Any]:
    db = deps.db()
    vs = deps.vs()

    brand_id = state.get("brand_id", "default")
    user_id = state.get("user_id", "default")
    brief = state.get("brief", "")

    # Load brand kit
    brand_kit_obj = db.get_brand_kit(brand_id)
    brand_kit = brand_kit_obj.model_dump() if brand_kit_obj else _default_brand_kit(brand_id)

    # Load user prefs
    prefs_obj = db.get_user_prefs(user_id)
    user_prefs = prefs_obj.model_dump() if prefs_obj else _default_user_prefs(user_id)

    # Retrieve similar past projects from vector store
    similar: list[dict] = []
    if brief:
        similar = vs.query(brief, n_results=3, where={"brand_id": brand_id} if brand_id != "default" else None)

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[memory_loader] brand={brand_id} user={user_id} similar_count={len(similar)}",
        }
    )

    return {
        "brand_kit": brand_kit,
        "user_prefs": user_prefs,
        "similar_projects": similar,
        "messages": messages,
    }


def _default_brand_kit(brand_id: str) -> dict:
    return {
        "brand_id": brand_id,
        "name": brand_id.replace("_", " ").title(),
        "logo": {"path": "assets/tong_sui_logo.png", "safe_area": "top_right"},
        "colors": {"primary": "#00B894", "secondary": "#FFFFFF", "accent": "#FF7675", "background": "#1A1A2E"},
        "fonts": {"title": "Poppins-SemiBold", "body": "Inter-Regular"},
        "subtitle_style": {
            "position": "bottom_center",
            "box_opacity": 0.55,
            "box_radius": 12,
            "padding_px": 14,
            "max_chars_per_line": 18,
            "highlight_keywords": True,
            "font_size": 38,
        },
        "intro_outro": {
            "intro_template": "mint_splash",
            "outro_cta": "Order now",
            "intro_duration_sec": 1.5,
            "outro_duration_sec": 2.0,
        },
    }


def _default_user_prefs(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "default_platform": "tiktok",
        "preferred_duration_sec": 20,
        "tone": ["fresh", "playful"],
        "pacing": "fast",
        "shot_density": 7,
        "cta_style": "soft",
    }
