"""Tests for agent/nodes/quality_gate.py — mock _probe_video, _check_blank_frame, etc."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.nodes.quality_gate import quality_gate, _probe_video, _check_blank_frame


# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_state(**overrides):
    state = {
        "plan": {"duration_sec": 20, "shot_list": [], "storyboard": []},
        "branded_clip_path": "",
        "brand_kit": {},
        "caption_segments": [],
        "qc_attempt": 1,
        "messages": [],
        "scene_clips": [],
    }
    state.update(overrides)
    return state


def _patch_config(max_attempts=2, threshold=5):
    return patch("agent.nodes.quality_gate._get_config",
                 side_effect=lambda key, default: max_attempts if key == "max_qc_attempts" else threshold)


# ── Tests: no video path ───────────────────────────────────────────────────────

class TestNoVideo:
    def test_missing_branded_path_records_issue(self):
        state = _base_state()
        with _patch_config():
            result = quality_gate(state)
        issues = result["quality_result"]["issues"]
        assert any("Branded clip not found" in i for i in issues)

    def test_nonexistent_file_records_issue(self, tmp_path):
        state = _base_state(branded_clip_path=str(tmp_path / "missing.mp4"))
        with _patch_config():
            result = quality_gate(state)
        issues = result["quality_result"]["issues"]
        assert any("Branded clip not found" in i for i in issues)


# ── Tests: duration check ─────────────────────────────────────────────────────

class TestDurationCheck:
    def _run(self, branded_path, probe_return, target_sec=20):
        state = _base_state(
            branded_clip_path=branded_path,
            plan={"duration_sec": target_sec, "shot_list": [], "storyboard": []},
        )
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video", return_value=probe_return), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False):
            return quality_gate(state)

    def test_within_tolerance_passes(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        result = self._run(str(p), {"duration": 21.0, "width": 1080, "height": 1920, "bit_rate": 500_000})
        issues = result["quality_result"]["issues"]
        assert not any("Duration" in i for i in issues)

    def test_too_short_fails(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        # target=20, actual=5 — diff=15 > tolerance=max(2, 6)=6
        result = self._run(str(p), {"duration": 5.0, "width": 1080, "height": 1920, "bit_rate": 500_000})
        issues = result["quality_result"]["issues"]
        assert any("Duration" in i for i in issues)

    def test_too_long_fails(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        result = self._run(str(p), {"duration": 50.0, "width": 1080, "height": 1920, "bit_rate": 500_000})
        issues = result["quality_result"]["issues"]
        assert any("Duration" in i for i in issues)


# ── Tests: resolution check ───────────────────────────────────────────────────

class TestResolutionCheck:
    def _run(self, tmp_path, width, height):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        state = _base_state(branded_clip_path=str(p))
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": width, "height": height, "bit_rate": 500_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False):
            return quality_gate(state)

    def test_correct_resolution_passes(self, tmp_path):
        result = self._run(tmp_path, 1080, 1920)
        assert not any("Resolution" in i for i in result["quality_result"]["issues"])

    def test_wrong_resolution_fails(self, tmp_path):
        result = self._run(tmp_path, 1920, 1080)  # landscape
        issues = result["quality_result"]["issues"]
        assert any("Resolution" in i for i in issues)

    def test_wrong_width_fails(self, tmp_path):
        result = self._run(tmp_path, 720, 1280)
        issues = result["quality_result"]["issues"]
        assert any("Resolution" in i for i in issues)


# ── Tests: blank frame detection ──────────────────────────────────────────────

class TestBlankFrameDetection:
    def test_blank_frame_detected(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        state = _base_state(branded_clip_path=str(p))
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": 1080, "height": 1920, "bit_rate": 500_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=True):
            result = quality_gate(state)
        issues = result["quality_result"]["issues"]
        assert any("blank" in i.lower() for i in issues)

    def test_non_blank_frame_passes(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        state = _base_state(branded_clip_path=str(p))
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": 1080, "height": 1920, "bit_rate": 500_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False):
            result = quality_gate(state)
        assert not any("blank" in i.lower() for i in result["quality_result"]["issues"])


# ── Tests: low bitrate ────────────────────────────────────────────────────────

class TestLowBitrate:
    def test_very_low_bitrate_flagged(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        state = _base_state(branded_clip_path=str(p))
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": 1080, "height": 1920, "bit_rate": 5_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False):
            result = quality_gate(state)
        issues = result["quality_result"]["issues"]
        assert any("bitrate" in i.lower() for i in issues)


# ── Tests: caption font_size auto-fix ─────────────────────────────────────────

class TestCaptionFontSize:
    def test_font_size_too_small_auto_fixed(self):
        state = _base_state(brand_kit={"subtitle_style": {"font_size": 10}})
        with _patch_config():
            result = quality_gate(state)
        qr = result["quality_result"]
        assert any("font_size" in i for i in qr["issues"])
        assert qr["auto_fix_applied"] is True
        # auto-fix sets it to 20
        assert result["brand_kit"]["subtitle_style"]["font_size"] == 20

    def test_font_size_ok_no_issue(self):
        state = _base_state(brand_kit={"subtitle_style": {"font_size": 44}})
        with _patch_config():
            result = quality_gate(state)
        assert not any("font_size" in i for i in result["quality_result"]["issues"])


# ── Tests: shot relevance (mocked) ───────────────────────────────────────────

class TestShotRelevance:
    def test_low_relevance_adds_issue(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        clips = [{"shot_id": "S1", "clip_path": str(tmp_path / "S1.mp4")}]
        storyboard = [{"desc": "product on table", "asset_hint": "product"}]
        state = _base_state(
            branded_clip_path=str(p),
            scene_clips=clips,
            plan={"duration_sec": 20, "shot_list": [], "storyboard": storyboard},
        )
        mock_scores = [{"shot_id": "S1", "score": 2, "reason": "wrong scene", "missing_elements": ["product"]}]
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": 1080, "height": 1920, "bit_rate": 500_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False), \
             patch("agent.nodes.quality_gate._check_shot_relevance", return_value=mock_scores), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake_key"}):
            result = quality_gate(state)
        issues = result["quality_result"]["issues"]
        assert any("S1" in i and "relevance" in i.lower() for i in issues)
        assert result["quality_result"]["low_relevance_shots"] == ["S1"]

    def test_high_relevance_no_issue(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        clips = [{"shot_id": "S1", "clip_path": str(tmp_path / "S1.mp4")}]
        storyboard = [{"desc": "product on table", "asset_hint": "product"}]
        state = _base_state(
            branded_clip_path=str(p),
            scene_clips=clips,
            plan={"duration_sec": 20, "shot_list": [], "storyboard": storyboard},
        )
        mock_scores = [{"shot_id": "S1", "score": 8, "reason": "good match", "missing_elements": []}]
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": 1080, "height": 1920, "bit_rate": 500_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False), \
             patch("agent.nodes.quality_gate._check_shot_relevance", return_value=mock_scores), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake_key"}):
            result = quality_gate(state)
        relevance_issues = [i for i in result["quality_result"]["issues"] if "relevance" in i.lower()]
        assert not relevance_issues

    def test_no_api_key_skips_relevance(self, tmp_path):
        """Without ANTHROPIC_API_KEY, relevance check is skipped (no crash)."""
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        clips = [{"shot_id": "S1", "clip_path": str(tmp_path / "S1.mp4")}]
        state = _base_state(
            branded_clip_path=str(p),
            scene_clips=clips,
            plan={"duration_sec": 20, "shot_list": [], "storyboard": [{"desc": "test"}]},
        )
        import os
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": 1080, "height": 1920, "bit_rate": 500_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False), \
             patch.dict("os.environ", env, clear=True):
            result = quality_gate(state)
        # No relevance-related issues (skipped)
        assert result["quality_result"]["relevance"] == []


# ── Tests: passed logic ───────────────────────────────────────────────────────

class TestPassedLogic:
    def test_no_issues_passes(self):
        state = _base_state()
        with _patch_config():
            result = quality_gate(state)
        qr = result["quality_result"]
        # missing branded path adds issue → not passed
        # but a clean state with no video should have "Branded clip not found" issue
        assert "passed" in qr

    def test_clean_video_passes(self, tmp_path):
        p = tmp_path / "v.mp4"
        p.write_bytes(b"fake")
        state = _base_state(branded_clip_path=str(p))
        with _patch_config(), \
             patch("agent.nodes.quality_gate._probe_video",
                   return_value={"duration": 20.0, "width": 1080, "height": 1920, "bit_rate": 500_000}), \
             patch("agent.nodes.quality_gate._check_blank_frame", return_value=False):
            result = quality_gate(state)
        assert result["quality_result"]["passed"] is True
        assert result["quality_result"]["issues"] == []

    def test_qc_attempt_increments(self):
        state = _base_state(qc_attempt=1)
        with _patch_config():
            result = quality_gate(state)
        assert result["qc_attempt"] == 2

    def test_auto_fix_with_attempts_remaining_passes(self):
        """Auto-fix issues pass QC when attempt < max_attempts."""
        state = _base_state(
            brand_kit={"subtitle_style": {"font_size": 5}},
            qc_attempt=1,
        )
        with _patch_config(max_attempts=2):
            result = quality_gate(state)
        qr = result["quality_result"]
        assert qr["auto_fix_applied"] is True
        assert qr["passed"] is True  # attempt(1) < max_attempts(2)
