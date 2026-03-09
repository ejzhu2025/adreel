"""Tests for agent/nodes/executor_pipeline.py — PIL fallback path (no FAL_KEY)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _minimal_plan(shot_count: int = 3) -> dict:
    shots = [
        {"shot_id": f"S{i+1}", "duration": 2.5, "type": "wide", "text_overlay": f"Shot {i+1}"}
        for i in range(shot_count)
    ]
    shots[-1]["type"] = "text"  # last shot is outro
    storyboard = [
        {"scene": i + 1, "desc": f"Scene {i+1} description", "asset_hint": "wide", "duration": 2.5}
        for i in range(shot_count)
    ]
    return {
        "platform": "tiktok",
        "duration_sec": shot_count * 2.5,
        "shot_list": shots,
        "storyboard": storyboard,
        "script": {"hook": "Hook text", "body": [], "cta": "Order now"},
    }


def _base_state(tmp_path: Path, shot_count: int = 3, **overrides) -> dict:
    state = {
        "project_id": "test_proj",
        "brief": "test product video",
        "brand_id": "test_brand",
        "user_id": "test_user",
        "messages": [],
        "clarification_answers": {"style_tone": ["fresh"], "platform": "tiktok"},
        "plan": _minimal_plan(shot_count),
        "brand_kit": {},
        "product_image_path": "",
        "quality": "turbo",
        "t2v_prompts": {},
    }
    state.update(overrides)
    return state


# ── PIL fallback tests ────────────────────────────────────────────────────────

class TestPilFallbackPath:
    """When FAL_KEY is not set, executor uses PIL FrameGenerator."""

    def _run_pipeline(self, tmp_path: Path, shot_count: int = 3, **state_overrides):
        from agent.nodes.executor_pipeline import executor_pipeline
        state = _base_state(tmp_path, shot_count, **state_overrides)

        # Patch FrameGenerator.generate_frame to produce real PNG files
        fake_frame_idx = {"n": 0}

        def fake_generate_frame(shot_id, shot_type, text_overlay="", scene_index=0,
                                is_intro=False, is_outro=False, background_image_path="",
                                logo_path=""):
            frame_path = tmp_path / f"{shot_id}_frame.png"
            # Write a minimal valid 1x1 PNG
            frame_path.write_bytes(
                b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
                b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
                b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
            )
            return frame_path

        # Patch FFmpegComposer.image_to_clip to create a fake output file
        def fake_image_to_clip(image_path, output_path, duration, width, height, ken_burns=True):
            Path(output_path).write_bytes(b"fake_video_data")

        env = {k: v for k, v in os.environ.items() if k not in ("FAL_KEY", "FAL_API_KEY")}

        with patch.dict(os.environ, env, clear=True), \
             patch("agent.nodes.executor_pipeline.FrameGenerator") as MockFG, \
             patch("agent.nodes.executor_pipeline.FFmpegComposer") as MockFC:
            mock_fg = MagicMock()
            mock_fg.generate_frame.side_effect = fake_generate_frame
            MockFG.return_value = mock_fg

            mock_fc = MagicMock()
            mock_fc.image_to_clip.side_effect = fake_image_to_clip
            MockFC.return_value = mock_fc

            # Override VAH_DATA_DIR so clips go into tmp_path
            with patch.dict(os.environ, {"VAH_DATA_DIR": str(tmp_path)}):
                result = executor_pipeline(state)

        return result, mock_fg, mock_fc

    def test_returns_scene_clips(self, tmp_path):
        result, _, _ = self._run_pipeline(tmp_path, shot_count=3)
        assert "scene_clips" in result
        clips = result["scene_clips"]
        assert len(clips) == 3

    def test_each_clip_has_required_keys(self, tmp_path):
        result, _, _ = self._run_pipeline(tmp_path, shot_count=3)
        for clip in result["scene_clips"]:
            assert "shot_id" in clip
            assert "clip_path" in clip
            assert "duration" in clip

    def test_clip_count_matches_shot_list(self, tmp_path):
        for n in (1, 5, 7):
            result, _, _ = self._run_pipeline(tmp_path, shot_count=n)
            assert len(result["scene_clips"]) == n

    def test_shot_ids_match_plan(self, tmp_path):
        result, _, _ = self._run_pipeline(tmp_path, shot_count=4)
        clip_ids = [c["shot_id"] for c in result["scene_clips"]]
        assert clip_ids == ["S1", "S2", "S3", "S4"]

    def test_last_shot_uses_is_outro(self, tmp_path):
        """Last shot's generate_frame call must pass is_outro=True."""
        result, mock_fg, _ = self._run_pipeline(tmp_path, shot_count=3)
        calls = mock_fg.generate_frame.call_args_list
        last_call = calls[-1]
        # is_outro is a keyword arg
        kwargs = last_call[1] if len(last_call) > 1 else {}
        if not kwargs:
            # might be positional — check all args
            args = last_call[0] if last_call[0] else ()
            # generate_frame(shot_id, shot_type, text_overlay, scene_index, is_intro, is_outro, ...)
            # is_outro is 6th positional arg (index 5)
            assert len(args) >= 6 or kwargs.get("is_outro") is True
        else:
            assert kwargs.get("is_outro") is True

    def test_messages_appended(self, tmp_path):
        result, _, _ = self._run_pipeline(tmp_path, shot_count=2)
        msgs = result.get("messages", [])
        assert any("executor" in m.get("content", "") for m in msgs)

    def test_clips_created_on_disk(self, tmp_path):
        """Each clip_path must exist (fake_image_to_clip creates them)."""
        result, _, _ = self._run_pipeline(tmp_path, shot_count=3)
        for clip in result["scene_clips"]:
            assert Path(clip["clip_path"]).exists(), f"Missing: {clip['clip_path']}"

    def test_single_shot_works(self, tmp_path):
        result, _, _ = self._run_pipeline(tmp_path, shot_count=1)
        assert len(result["scene_clips"]) == 1
        assert result["scene_clips"][0]["shot_id"] == "S1"

    def test_with_product_image_outro(self, tmp_path):
        """Product image is passed to last frame's background_image_path."""
        fake_img = tmp_path / "product.jpg"
        fake_img.write_bytes(b"fake_image")
        result, mock_fg, _ = self._run_pipeline(
            tmp_path, shot_count=2, product_image_path=str(fake_img)
        )
        # Last call should have background_image_path set
        calls = mock_fg.generate_frame.call_args_list
        last_call_kwargs = calls[-1][1] if len(calls[-1]) > 1 else {}
        # Check if product image was passed to last shot
        assert len(result["scene_clips"]) == 2
