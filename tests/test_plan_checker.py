"""Tests for plan_checker node logic."""
import pytest
from agent.nodes.plan_checker import plan_checker, _align_shots_to_storyboard


def _make_state(
    n_shots: int = 5,
    scene_duration: float = 4.0,
    target_duration: int = 20,
    include_hook: bool = True,
    include_cta: bool = True,
) -> dict:
    storyboard = [
        {"scene": i + 1, "desc": f"Scene {i+1}", "duration": scene_duration}
        for i in range(n_shots)
    ]
    shot_list = [
        {"shot_id": f"S{i+1}", "type": "wide", "asset": "generate", "text_overlay": "", "duration": scene_duration}
        for i in range(n_shots)
    ]
    return {
        "plan": {
            "project_id": "test",
            "duration_sec": target_duration,
            "storyboard": storyboard,
            "shot_list": shot_list,
            "script": {
                "hook": "Test hook" if include_hook else "",
                "body": ["body line"],
                "cta": "Order now" if include_cta else "",
            },
            "render_targets": ["9:16"],
        },
        "messages": [],
        "plan_version": 1,
    }


class TestPlanChecker:
    def test_valid_plan_passes(self):
        state = _make_state(n_shots=5, scene_duration=4.0, target_duration=20)
        result = plan_checker(state)
        assert not result["needs_replan"]

    def test_duration_within_tolerance_passes(self):
        # 5 scenes × 4.0s = 20s, target 20s — passes
        state = _make_state(n_shots=5, scene_duration=4.0, target_duration=20)
        result = plan_checker(state)
        assert not result["needs_replan"]

    def test_duration_mismatch_fails(self):
        # 5 × 4.0 = 20s vs target 30s — delta=10s > 3s threshold
        state = _make_state(n_shots=5, scene_duration=4.0, target_duration=30)
        result = plan_checker(state)
        assert result["needs_replan"] is True

    def test_auto_adjusts_last_scene_for_small_delta(self):
        # 5 × 3.8 = 19s vs target 20s — delta=1s ≤ 3s, auto-fix
        state = _make_state(n_shots=5, scene_duration=3.8, target_duration=20)
        result = plan_checker(state)
        assert not result["needs_replan"]
        # Last scene should be extended
        last_dur = result["plan"]["storyboard"][-1]["duration"]
        total = sum(s["duration"] for s in result["plan"]["storyboard"])
        assert abs(total - 20) < 0.5

    def test_missing_hook_triggers_replan(self):
        state = _make_state(include_hook=False)
        result = plan_checker(state)
        assert result["needs_replan"] is True

    def test_missing_cta_triggers_replan(self):
        state = _make_state(include_cta=False)
        result = plan_checker(state)
        assert result["needs_replan"] is True

    def test_empty_shot_list_triggers_replan(self):
        state = _make_state()
        state["plan"]["shot_list"] = []
        result = plan_checker(state)
        assert result["needs_replan"] is True

    def test_mismatched_shots_storyboard_auto_padded(self):
        # 3 shots but 5 scenes — should auto-pad
        state = _make_state(n_shots=5)
        state["plan"]["shot_list"] = state["plan"]["shot_list"][:3]
        result = plan_checker(state)
        # After fix, shot_list should match storyboard length
        assert len(result["plan"]["shot_list"]) == len(result["plan"]["storyboard"])

    def test_render_targets_default_added(self):
        state = _make_state()
        state["plan"]["render_targets"] = []
        result = plan_checker(state)
        assert "9:16" in result["plan"]["render_targets"]


class TestAlignShotsToStoryboard:
    def test_pads_short_shot_list(self):
        shots = [{"shot_id": "S1", "type": "wide", "asset": "x", "text_overlay": "", "duration": 3.0}]
        storyboard = [
            {"scene": 1, "desc": "A", "duration": 3.0, "asset_hint": "macro"},
            {"scene": 2, "desc": "B", "duration": 3.0, "asset_hint": "close"},
            {"scene": 3, "desc": "C", "duration": 3.0, "asset_hint": "text"},
        ]
        result = _align_shots_to_storyboard(shots, storyboard)
        assert len(result) == 3
        assert result[1]["shot_id"] == "S2"

    def test_trims_long_shot_list(self):
        shots = [
            {"shot_id": f"S{i}", "type": "wide", "asset": "x", "text_overlay": "", "duration": 2.0}
            for i in range(6)
        ]
        storyboard = [{"scene": i + 1, "desc": "", "duration": 2.0} for i in range(3)]
        result = _align_shots_to_storyboard(shots, storyboard)
        assert len(result) == 3
