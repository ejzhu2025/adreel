"""Tests for agent/nodes/intent_parser.py — extract signals from brief."""
from __future__ import annotations

import pytest

from agent.nodes.intent_parser import intent_parser


def _run(brief: str, pre: dict | None = None) -> dict:
    state = {"brief": brief, "clarification_answers": pre or {}, "messages": []}
    return intent_parser(state)


# ── Duration extraction ───────────────────────────────────────────────────────

class TestDurationExtraction:
    def test_seconds_short(self):
        out = _run("Make a 15s promo video")
        assert out["clarification_answers"]["duration_sec"] == 15

    def test_seconds_full_word(self):
        out = _run("Create a 30 second ad")
        assert out["clarification_answers"]["duration_sec"] == 30

    def test_seconds_plural(self):
        out = _run("Generate a 20 seconds clip")
        assert out["clarification_answers"]["duration_sec"] == 20

    def test_no_duration_not_set(self):
        out = _run("Make a promo video for my coffee shop")
        assert "duration_sec" not in out["clarification_answers"]

    def test_existing_duration_not_overwritten(self):
        """Pre-filled answers must not be overwritten by parser."""
        out = _run("Make a 10s video", pre={"duration_sec": 25})
        assert out["clarification_answers"]["duration_sec"] == 25


# ── Platform detection ────────────────────────────────────────────────────────

class TestPlatformDetection:
    def test_tiktok(self):
        out = _run("This is for TikTok")
        assert out["clarification_answers"]["platform"] == "tiktok"

    def test_instagram_reels(self):
        out = _run("Post on Instagram Reels")
        assert out["clarification_answers"]["platform"] == "reels"

    def test_youtube_shorts(self):
        out = _run("YouTube Shorts video")
        assert out["clarification_answers"]["platform"] == "shorts"

    def test_ig_abbreviation(self):
        out = _run("IG story promo")
        assert out["clarification_answers"]["platform"] == "reels"

    def test_no_platform_not_set(self):
        out = _run("Make a cool product video")
        assert "platform" not in out["clarification_answers"]


# ── Language detection ────────────────────────────────────────────────────────

class TestLanguageDetection:
    def test_chinese_brief(self):
        out = _run("请制作一个夏季饮料的推广视频")
        assert out["clarification_answers"]["language"] == "zh"

    def test_english_brief(self):
        out = _run("Create a summer drink promo video")
        assert out["clarification_answers"]["language"] == "en"

    def test_mixed_language_detected_as_zh(self):
        out = _run("Create a 椰子watermelon drink video for TikTok")
        assert out["clarification_answers"]["language"] == "zh"

    def test_language_always_present(self):
        """language key must always be set regardless of brief content."""
        out = _run("anything")
        assert "language" in out["clarification_answers"]


# ── Tone extraction ───────────────────────────────────────────────────────────

class TestToneExtraction:
    def test_fresh_tone(self):
        out = _run("A fresh summer coconut watermelon drink")
        assert "fresh" in out["clarification_answers"].get("style_tone", [])

    def test_premium_tone(self):
        out = _run("Luxury premium artisan coffee brand")
        assert "premium" in out["clarification_answers"].get("style_tone", [])

    def test_no_tone_key_absent(self):
        out = _run("Make a video about tables")
        assert "style_tone" not in out["clarification_answers"]

    def test_multiple_tones(self):
        out = _run("A fun playful summer promo deal")
        tones = out["clarification_answers"].get("style_tone", [])
        assert len(tones) >= 2


# ── Messages ─────────────────────────────────────────────────────────────────

class TestMessages:
    def test_appends_system_message(self):
        out = _run("Test brief")
        assert any(m["role"] == "system" for m in out["messages"])

    def test_message_contains_intent_parser_tag(self):
        out = _run("Test brief")
        system_msgs = [m for m in out["messages"] if m["role"] == "system"]
        assert any("intent_parser" in m["content"] for m in system_msgs)
