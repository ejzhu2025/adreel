"""Tests for agent/nodes/music_mixer.py — tone selection and graceful skip.

Bug regressions:
- style_tone removed from UI; music must infer from concept.mood / brief
- Missing REPLICATE_API_TOKEN must skip silently, not crash
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agent.nodes.music_mixer import _select_tone, music_mixer


# ── _select_tone ──────────────────────────────────────────────────────────────

class TestSelectTone:
    def test_energetic_brief_gives_upbeat(self):
        assert _select_tone("energetic fast fitness dance", "") == "upbeat"

    def test_luxury_brief_gives_dramatic(self):
        assert _select_tone("bold premium luxury launch", "") == "dramatic"

    def test_cozy_brief_gives_warm(self):
        assert _select_tone("cozy warm coffee dessert", "") == "warm"

    def test_calm_brief_gives_chill(self):
        assert _select_tone("calm relaxing wellness skincare", "") == "chill"

    def test_no_match_defaults_to_warm(self):
        assert _select_tone("table chair rock", "") == "warm"

    def test_style_tone_overrides_brief(self):
        """style_tone is repeated 3× in the scored text, so 1 style_tone keyword × 3 > 2 brief keywords.

        text = "bold bold bold cozy warm" → dramatic:bold=3 vs warm:cozy+warm=2 → dramatic wins.
        """
        tone = _select_tone("cozy warm", "bold")  # bold×3=3 > cozy+warm=2
        assert tone == "dramatic"

    def test_style_tone_list_joined(self):
        tone = _select_tone("general product video", ["energetic", "fun"])
        assert tone == "upbeat"

    def test_empty_inputs_default_warm(self):
        assert _select_tone("", "") == "warm"

    def test_case_insensitive(self):
        assert _select_tone("ENERGETIC FAST", "") == "upbeat"


# ── music_mixer node ──────────────────────────────────────────────────────────

class TestMusicMixerNode:
    def test_no_replicate_token_returns_empty(self):
        """Must skip gracefully if REPLICATE_API_TOKEN not set."""
        state = {
            "brief": "summer drink promo",
            "plan": {},
            "branded_clip_path": "",
        }
        with patch.dict(os.environ, {}, clear=False):
            env = {k: v for k, v in os.environ.items() if k != "REPLICATE_API_TOKEN"}
            with patch.dict(os.environ, env, clear=True):
                result = music_mixer(state)
        assert result == {"music_track_path": ""}

    def test_missing_branded_path_returns_empty(self, tmp_path):
        """Must skip if branded_clip_path does not exist on disk."""
        state = {
            "brief": "summer drink",
            "plan": {},
            "branded_clip_path": str(tmp_path / "nonexistent.mp4"),
        }
        with patch.dict(os.environ, {"REPLICATE_API_TOKEN": "fake_token"}):
            result = music_mixer(state)
        assert result == {"music_track_path": ""}

    def test_empty_branded_path_returns_empty(self):
        state = {
            "brief": "summer drink",
            "plan": {},
            "branded_clip_path": "",
        }
        with patch.dict(os.environ, {"REPLICATE_API_TOKEN": "fake_token"}):
            result = music_mixer(state)
        assert result == {"music_track_path": ""}

    def test_uses_concept_mood_not_style_tone(self):
        """After style_tone chip removal, music must read plan.concept.mood."""
        # style_tone is NOT in state — music must not crash
        state = {
            "brief": "luxury premium brand launch",
            "plan": {"concept": {"mood": "dramatic", "visual_style": "cinematic"}},
            "branded_clip_path": "",  # will skip early
        }
        with patch.dict(os.environ, {"REPLICATE_API_TOKEN": "fake_token"}):
            result = music_mixer(state)
        # Skips because branded_clip_path missing — but must not raise KeyError for style_tone
        assert "music_track_path" in result
