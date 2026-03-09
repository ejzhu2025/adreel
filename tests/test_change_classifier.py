"""Tests for agent/nodes/change_classifier.py — LLM-based change classification.

Bug regressions:
- No ANTHROPIC_API_KEY must fall back to heuristic, not crash
- affected_shot_indices must only contain valid indices (< len(shot_list))
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agent.nodes.change_classifier import change_classifier, _call_anthropic


def _make_state(feedback: str, shot_count: int = 3) -> dict:
    storyboard = [{"desc": f"Scene {i} description", "duration": 3.0} for i in range(shot_count)]
    shot_list = [{"shot_id": f"S{i+1}", "text_overlay": f"Shot {i+1}"} for i in range(shot_count)]
    return {
        "brief": "Summer coconut watermelon promo",
        "plan": {
            "platform": "tiktok",
            "duration_sec": 10,
            "style_tone": ["fresh"],
            "script": {"hook": "Cool down this summer"},
            "storyboard": storyboard,
            "shot_list": shot_list,
        },
        "plan_feedback": feedback,
        "messages": [],
    }


# ── Heuristic fallback (no API key) ──────────────────────────────────────────

class TestHeuristicFallback:
    def _run_no_key(self, feedback: str, shot_count: int = 3) -> dict:
        state = _make_state(feedback, shot_count)
        with patch.dict(os.environ, {}, clear=False):
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                return change_classifier(state)

    def test_global_keyword_triggers_global(self):
        result = self._run_no_key("Change the entire style and mood")
        assert result["change_type"] == "global"
        assert result["affected_shot_indices"] == []

    def test_no_global_keyword_triggers_local(self):
        result = self._run_no_key("Make the product look more vibrant in shot 2")
        assert result["change_type"] == "local"

    def test_redo_keyword_triggers_global(self):
        result = self._run_no_key("Please redo the whole video")
        assert result["change_type"] == "global"

    def test_narrative_keyword_triggers_global(self):
        result = self._run_no_key("Restructure the narrative flow")
        assert result["change_type"] == "global"

    def test_no_crash_without_api_key(self):
        """Core regression: missing API key must not raise."""
        result = self._run_no_key("Change shot 1 to show the drink pouring")
        assert "change_type" in result
        assert "affected_shot_indices" in result

    def test_local_fallback_returns_all_indices(self):
        """Heuristic local: returns all shot indices (conservative — regen all)."""
        result = self._run_no_key("Fix the text overlay on shot 2", shot_count=3)
        assert result["change_type"] == "local"
        # Heuristic returns range(len(storyboard))
        assert set(result["affected_shot_indices"]) == {0, 1, 2}


# ── LLM response parsing (mock Anthropic) ────────────────────────────────────

class TestLLMResponseParsing:
    def _run_with_mock_llm(self, llm_json: dict, feedback: str = "fix shot 1") -> dict:
        state = _make_state(feedback, shot_count=3)
        with patch("agent.nodes.change_classifier._call_anthropic", return_value=llm_json):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake_key"}):
                return change_classifier(state)

    def test_local_change_parsed(self):
        result = self._run_with_mock_llm({
            "change_type": "local",
            "reasoning": "Only shot 1 needs updating",
            "affected_shot_indices": [1],
            "shot_updates": {"1": {"desc": "New description", "text_overlay": "New text"}},
        })
        assert result["change_type"] == "local"
        assert result["affected_shot_indices"] == [1]
        assert "1" in result["shot_updates"]

    def test_global_change_parsed(self):
        result = self._run_with_mock_llm({
            "change_type": "global",
            "reasoning": "Style change needed",
            "affected_shot_indices": [],
            "shot_updates": {},
        })
        assert result["change_type"] == "global"
        assert result["affected_shot_indices"] == []

    def test_string_indices_converted_to_int(self):
        """LLM may return indices as strings — must be converted to int."""
        result = self._run_with_mock_llm({
            "change_type": "local",
            "reasoning": "",
            "affected_shot_indices": ["0", "2"],
            "shot_updates": {},
        })
        assert all(isinstance(i, int) for i in result["affected_shot_indices"])

    def test_shot_updates_keys_are_strings(self):
        result = self._run_with_mock_llm({
            "change_type": "local",
            "reasoning": "",
            "affected_shot_indices": [0],
            "shot_updates": {0: {"desc": "new"}},  # int key from LLM
        })
        assert all(isinstance(k, str) for k in result["shot_updates"])

    def test_llm_error_falls_back_to_heuristic(self):
        """If _call_anthropic returns None (error), fallback must activate."""
        state = _make_state("Change the whole style")
        with patch("agent.nodes.change_classifier._call_anthropic", return_value=None):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake_key"}):
                result = change_classifier(state)
        assert result["change_type"] in ("global", "local")


# ── Output structure ─────────────────────────────────────────────────────────

class TestOutputStructure:
    def _run(self, feedback: str) -> dict:
        state = _make_state(feedback)
        with patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}, clear=True):
            return change_classifier(state)

    def test_always_has_change_type(self):
        result = self._run("anything")
        assert "change_type" in result

    def test_always_has_affected_indices(self):
        result = self._run("anything")
        assert "affected_shot_indices" in result
        assert isinstance(result["affected_shot_indices"], list)

    def test_always_has_shot_updates(self):
        result = self._run("anything")
        assert "shot_updates" in result
        assert isinstance(result["shot_updates"], dict)

    def test_always_appends_message(self):
        result = self._run("anything")
        assert any("change_classifier" in m.get("content", "") for m in result["messages"])
