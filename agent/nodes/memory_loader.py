"""memory_loader — fetch brand kit, user prefs, and similar past projects."""
from __future__ import annotations

from typing import Any

import agent.deps as deps


def memory_loader(state: dict[str, Any]) -> dict[str, Any]:
    db = deps.db()
    vs = deps.vs()

    user_id = state.get("user_id", "default")
    brief = state.get("brief", "")
    brand_id = state.get("brand_id", "default")

    # Build brand_kit from scraped product info (no DB brand kit lookup)
    product_info = state.get("product_info") or {}
    brand_info = product_info.get("brand_info") or {}
    brand_kit = _brand_kit_from_product_info(brand_info, brief)

    # Load user prefs
    prefs_obj = db.get_user_prefs(user_id)
    user_prefs = prefs_obj.model_dump() if prefs_obj else _default_user_prefs(user_id)

    # Retrieve similar past projects from vector store
    similar: list[dict] = []
    if brief:
        similar = vs.query(brief, n_results=3)

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": f"[memory_loader] brand={brand_kit.get('name', 'auto')} user={user_id} similar_count={len(similar)}",
    })

    return {
        "brand_kit": brand_kit,
        "user_prefs": user_prefs,
        "similar_projects": similar,
        "messages": messages,
    }


def _brand_kit_from_product_info(brand_info: dict, brief: str = "") -> dict:
    """Build a minimal brand_kit from scraped URL brand info.
    No Tong Sui defaults — colors/name come from the actual product page.
    """
    primary = brand_info.get("primary_color") or _infer_color_from_brief(brief)
    name = brand_info.get("brand_name") or ""
    logo_path = brand_info.get("logo_path") or ""

    return {
        "brand_id": "auto",
        "name": name,
        "logo": {"path": logo_path, "safe_area": "top_right"},
        "colors": {
            "primary": primary,
            "secondary": "#FFFFFF",
            "accent": primary,
            "background": "#111111",
        },
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
            "intro_template": "clean",
            "outro_cta": "Shop now",
            "intro_duration_sec": 1.5,
            "outro_duration_sec": 2.0,
        },
    }


def _infer_color_from_brief(brief: str) -> str:
    """Infer a neutral brand color from brief keywords when no product image color is available."""
    brief_lower = brief.lower()
    if any(w in brief_lower for w in ["sport", "gym", "run", "fitness", "workout", "athletic"]):
        return "#1a1a1a"  # dark/bold for sports
    if any(w in brief_lower for w in ["luxury", "gold", "premium", "jewelry", "watch"]):
        return "#c9a84c"  # gold
    if any(w in brief_lower for w in ["skin", "beauty", "cream", "serum", "glow"]):
        return "#f5e6d8"  # warm blush
    if any(w in brief_lower for w in ["tech", "app", "software", "digital"]):
        return "#2563eb"  # blue
    return "#333333"  # neutral dark


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
