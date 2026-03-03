"""plan_checker — validate the generated plan against brand and duration constraints."""
from __future__ import annotations

from typing import Any


def plan_checker(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    issues: list[str] = []
    needs_replan = False

    # 1. Duration check
    target_sec = int(plan.get("duration_sec", 20))
    storyboard = plan.get("storyboard", [])
    actual_sec = round(sum(s.get("duration", 0) for s in storyboard), 2)
    if abs(actual_sec - target_sec) > 3:
        issues.append(
            f"Duration mismatch: storyboard={actual_sec}s, target={target_sec}s (delta>{3}s)"
        )
        needs_replan = True

    # 2. Shot list completeness
    shot_list = plan.get("shot_list", [])
    if not shot_list:
        issues.append("shot_list is empty")
        needs_replan = True

    if len(shot_list) != len(storyboard):
        issues.append(
            f"shot_list length ({len(shot_list)}) != storyboard length ({len(storyboard)})"
        )
        # Auto-fix by padding/trimming shot_list
        shot_list = _align_shots_to_storyboard(shot_list, storyboard)
        plan["shot_list"] = shot_list

    # 3. Script completeness
    script = plan.get("script", {})
    if not script.get("hook"):
        issues.append("script.hook is missing")
        needs_replan = True
    if not script.get("cta"):
        issues.append("script.cta is missing")
        needs_replan = True

    # 4. Render targets
    if not plan.get("render_targets"):
        plan["render_targets"] = ["9:16"]

    # 5. Auto-fix duration if off by ≤3s (stretch/compress last scene)
    if issues and not needs_replan:
        pass
    if actual_sec != target_sec and abs(actual_sec - target_sec) <= 3 and storyboard:
        delta = round(target_sec - actual_sec, 2)
        storyboard[-1]["duration"] = round(storyboard[-1]["duration"] + delta, 2)
        if storyboard[-1].get("duration", 0) < 0.5:
            storyboard[-1]["duration"] = 0.5
        plan["storyboard"] = storyboard
        # Update shot durations too
        for shot in shot_list:
            if shot["shot_id"] == storyboard[-1].get("shot_id") or shot is shot_list[-1]:
                shot["duration"] = storyboard[-1]["duration"]
        issues = [i for i in issues if "Duration" not in i]

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[plan_checker] issues={issues} needs_replan={needs_replan}",
        }
    )

    return {
        "plan": plan,
        "needs_replan": needs_replan,
        "messages": messages,
    }


def _align_shots_to_storyboard(shots: list[dict], storyboard: list[dict]) -> list[dict]:
    """Pad or trim shot_list to match storyboard length."""
    result = list(shots)
    for i in range(len(shots), len(storyboard)):
        scene = storyboard[i]
        result.append({
            "shot_id": f"S{scene['scene']}",
            "type": scene.get("asset_hint", "wide"),
            "asset": "generate",
            "text_overlay": "",
            "duration": scene.get("duration", 2.5),
        })
    return result[: len(storyboard)]
