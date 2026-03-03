"""caption_agent — build subtitle segments from script with proportional timing."""
from __future__ import annotations

import re
from typing import Any


def caption_agent(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    brand_kit = state.get("brand_kit", {})
    script = plan.get("script", {})
    shot_list = plan.get("shot_list", [])
    duration_sec = int(plan.get("duration_sec", 20))
    max_chars = brand_kit.get("subtitle_style", {}).get("max_chars_per_line", 18)

    # Collect all script lines in order: hook + body + cta
    all_lines: list[str] = []
    if script.get("hook"):
        all_lines.append(script["hook"])
    all_lines.extend(script.get("body", []))
    if script.get("cta"):
        all_lines.append(script["cta"])

    # Also pull text_overlay lines from shot_list (non-empty unique ones)
    overlay_lines = [s["text_overlay"] for s in shot_list if s.get("text_overlay")]
    # Merge: prefer script lines; supplement with overlay if script is sparse
    if len(all_lines) < len(shot_list):
        for ol in overlay_lines:
            if ol not in all_lines:
                all_lines.append(ol)

    # Map lines to time segments proportional to shot durations
    shot_durations = [float(s.get("duration", 2.5)) for s in shot_list]
    if not shot_durations:
        shot_durations = [duration_sec / max(len(all_lines), 1)] * len(all_lines)

    total_dur = sum(shot_durations)
    cumulative = 0.0

    segments: list[dict] = []
    for idx, (line, dur) in enumerate(zip(all_lines, shot_durations)):
        start = cumulative
        end = cumulative + dur
        wrapped = _wrap_text(line, max_chars)
        # Detect keywords to highlight (ALL CAPS or important nouns)
        highlights = _extract_highlights(line)
        segments.append(
            {
                "index": idx + 1,
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "text": wrapped,
                "highlighted_words": highlights,
            }
        )
        cumulative += dur

    # If extra lines remain beyond shot count, append at end
    for idx, line in enumerate(all_lines[len(shot_durations):], start=len(segments) + 1):
        segments.append(
            {
                "index": idx,
                "start_sec": round(total_dur - 1.5, 3),
                "end_sec": round(total_dur, 3),
                "text": _wrap_text(line, max_chars),
                "highlighted_words": _extract_highlights(line),
            }
        )

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[caption_agent] {len(segments)} caption segments generated",
        }
    )

    return {"caption_segments": segments, "messages": messages}


def _wrap_text(text: str, max_chars: int) -> str:
    """Break text into lines of max_chars."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return "\n".join(lines)


def _extract_highlights(text: str) -> list[str]:
    """Simple heuristic: highlight all-caps words and emoji-adjacent words."""
    words = text.split()
    highlights = []
    for word in words:
        clean = re.sub(r"[^\w]", "", word)
        if clean.isupper() and len(clean) > 1:
            highlights.append(word)
        elif "%" in word or "$" in word or "#" in word:
            highlights.append(word)
    return highlights
