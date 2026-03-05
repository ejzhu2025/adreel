"""prompt_adherence — check if plan covers expected keywords. Text-based, no API."""
from __future__ import annotations

from typing import Any


def score(plan: dict[str, Any], expected_keywords: list[str]) -> dict[str, Any]:
    """
    Check each expected keyword against the plan text corpus.

    Corpus = storyboard[*].desc + shot_list[*].text_overlay + script hook/body/cta.
    Score = matched / total keywords (0.0–1.0).
    """
    if not expected_keywords:
        return {"score": 1.0, "matched": [], "missing": []}

    corpus_parts: list[str] = []

    # storyboard descriptions
    for shot in plan.get("storyboard", []):
        if isinstance(shot, dict):
            corpus_parts.append(shot.get("desc", ""))
            corpus_parts.append(shot.get("description", ""))

    # shot_list text overlays
    for shot in plan.get("shot_list", []):
        if isinstance(shot, dict):
            corpus_parts.append(shot.get("text_overlay", ""))
            corpus_parts.append(shot.get("desc", ""))
            corpus_parts.append(shot.get("description", ""))

    # script sections
    script = plan.get("script", {})
    if isinstance(script, dict):
        corpus_parts.append(script.get("hook", ""))
        corpus_parts.append(script.get("body", ""))
        corpus_parts.append(script.get("cta", ""))
    elif isinstance(script, str):
        corpus_parts.append(script)

    # brief itself if present
    corpus_parts.append(plan.get("brief", ""))
    corpus_parts.append(plan.get("title", ""))

    corpus = " ".join(corpus_parts).lower()

    matched: list[str] = []
    missing: list[str] = []
    for kw in expected_keywords:
        if kw.lower() in corpus:
            matched.append(kw)
        else:
            missing.append(kw)

    adherence_score = len(matched) / len(expected_keywords)
    return {"score": adherence_score, "matched": matched, "missing": missing}
