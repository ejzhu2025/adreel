"""Tests for Pydantic schemas and JSON serialization."""
import json
import pytest
from memory.schemas import (
    BrandKit, UserPrefs, Plan, Script, StoryboardScene, Shot,
    CaptionSegment, QualityCheckResult,
)


class TestBrandKit:
    def test_default_construction(self):
        kit = BrandKit(brand_id="test")
        assert kit.brand_id == "test"
        assert kit.colors.primary == "#00B894"
        assert kit.subtitle_style.font_size == 38

    def test_serialization_round_trip(self):
        kit = BrandKit(brand_id="acme", name="Acme Corp")
        json_str = kit.model_dump_json()
        restored = BrandKit.model_validate_json(json_str)
        assert restored.brand_id == kit.brand_id
        assert restored.name == kit.name

    def test_tong_sui_kit(self):
        kit = BrandKit(
            brand_id="tong_sui",
            name="Tong Sui",
            colors={"primary": "#00B894", "secondary": "#FFFFFF", "accent": "#FF7675", "background": "#1A1A2E"},
            subtitle_style={"position": "bottom_center", "box_opacity": 0.55, "font_size": 44},
        )
        assert kit.colors.primary == "#00B894"
        assert kit.subtitle_style.box_opacity == 0.55


class TestUserPrefs:
    def test_default_construction(self):
        prefs = UserPrefs(user_id="ej")
        assert prefs.default_platform == "tiktok"
        assert prefs.preferred_duration_sec == 20

    def test_custom_tone(self):
        prefs = UserPrefs(user_id="test", tone=["premium", "minimal"])
        assert "premium" in prefs.tone


class TestPlan:
    def _make_plan(self, n_shots: int = 3, duration: int = 9) -> Plan:
        storyboard = [
            StoryboardScene(scene=i+1, desc=f"Scene {i+1}", duration=3.0)
            for i in range(n_shots)
        ]
        shot_list = [
            Shot(shot_id=f"S{i+1}", type="wide", asset="generate", text_overlay=f"Text {i+1}")
            for i in range(n_shots)
        ]
        return Plan(
            project_id="p001",
            script=Script(hook="Hook!", body=["Line 1", "Line 2"], cta="Order now"),
            storyboard=storyboard,
            shot_list=shot_list,
            duration_sec=duration,
        )

    def test_plan_construction(self):
        plan = self._make_plan()
        assert plan.project_id == "p001"
        assert len(plan.storyboard) == 3
        assert plan.script.hook == "Hook!"

    def test_plan_json_serialization(self):
        plan = self._make_plan()
        d = plan.model_dump()
        assert "script" in d
        assert "storyboard" in d
        assert "shot_list" in d
        # Re-parse
        plan2 = Plan.model_validate(d)
        assert plan2.project_id == plan.project_id

    def test_storyboard_duration_sum(self):
        plan = self._make_plan(n_shots=5, duration=15)
        total = sum(s.duration for s in plan.storyboard)
        assert total == pytest.approx(15.0, abs=0.1)


class TestCaptionSegment:
    def test_segment(self):
        seg = CaptionSegment(index=1, start_sec=0.0, end_sec=3.0, text="Hello world")
        assert seg.start_sec == 0.0
        assert seg.end_sec == 3.0

    def test_highlighted_words(self):
        seg = CaptionSegment(
            index=1, start_sec=0.0, end_sec=2.0,
            text="100% NATURAL", highlighted_words=["100%", "NATURAL"]
        )
        assert "NATURAL" in seg.highlighted_words


class TestQualityCheckResult:
    def test_passed(self):
        qr = QualityCheckResult(passed=True)
        assert qr.passed is True
        assert qr.issues == []

    def test_failed_with_issues(self):
        qr = QualityCheckResult(passed=False, issues=["Duration too long"])
        assert not qr.passed
        assert len(qr.issues) == 1
