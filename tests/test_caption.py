"""Tests for caption_agent helpers + caption_renderer.

Covers:
- _wrap_text word-boundary logic
- _extract_highlights all-caps / special chars
- CaptionRenderer.write_srt  timestamp format + min duration
- CaptionRenderer.write_ass  section headers + style embedding
"""
from __future__ import annotations

import os
import tempfile

import pytest

from agent.nodes.caption_agent import _extract_highlights, _wrap_text
from render.caption_renderer import CaptionRenderer, _format_time, _ass_time


# ── _wrap_text ────────────────────────────────────────────────────────────────

class TestWrapText:
    def test_short_text_unchanged(self):
        assert _wrap_text("Hello world", 40) == "Hello world"

    def test_breaks_at_word_boundary(self):
        result = _wrap_text("one two three four five six", 15)
        for line in result.split("\n"):
            assert len(line) <= 15

    def test_no_mid_word_break(self):
        result = _wrap_text("superlongwordthatexceedslimit short", 20)
        # long word appears intact on its own line
        assert "superlongwordthatexceedslimit" in result

    def test_empty_string(self):
        assert _wrap_text("", 20) == ""

    def test_single_word(self):
        assert _wrap_text("Hello", 5) == "Hello"

    def test_multiline_output(self):
        result = _wrap_text("a b c d e f g h i j k l", 6)
        assert "\n" in result


# ── _extract_highlights ───────────────────────────────────────────────────────

class TestExtractHighlights:
    def test_all_caps_word_extracted(self):
        assert "NEW" in _extract_highlights("Try our NEW formula")

    def test_lowercase_not_extracted(self):
        result = _extract_highlights("this is a normal sentence")
        assert result == []

    def test_percent_extracted(self):
        assert "50%" in _extract_highlights("Get 50% off today")

    def test_dollar_extracted(self):
        assert "$5" in _extract_highlights("Only $5 today")

    def test_hashtag_extracted(self):
        result = _extract_highlights("Follow us #SUMMER")
        assert any("#" in h for h in result)

    def test_single_char_caps_ignored(self):
        result = _extract_highlights("I am A great person")
        assert "A" not in result  # single char uppercase ignored

    def test_empty_string(self):
        assert _extract_highlights("") == []


# ── _format_time (SRT) ────────────────────────────────────────────────────────

class TestFormatTime:
    def test_zero(self):
        assert _format_time(0.0) == "00:00:00,000"

    def test_one_second(self):
        assert _format_time(1.0) == "00:00:01,000"

    def test_with_milliseconds(self):
        assert _format_time(1.5) == "00:00:01,500"

    def test_minutes(self):
        assert _format_time(90.0) == "00:01:30,000"

    def test_hours(self):
        assert _format_time(3661.0) == "01:01:01,000"


# ── CaptionRenderer.write_srt ─────────────────────────────────────────────────

class TestWriteSrt:
    @pytest.fixture()
    def renderer(self):
        return CaptionRenderer()

    @pytest.fixture()
    def tmp_srt(self, tmp_path):
        return str(tmp_path / "out.srt")

    def test_basic_srt_structure(self, renderer, tmp_srt):
        segs = [
            {"index": 1, "start_sec": 0.0, "end_sec": 2.0, "text": "Hello world"},
            {"index": 2, "start_sec": 2.0, "end_sec": 4.0, "text": "Goodbye"},
        ]
        renderer.write_srt(segs, tmp_srt)
        content = open(tmp_srt).read()
        assert "00:00:00,000 --> 00:00:02,000" in content
        assert "Hello world" in content
        assert "Goodbye" in content

    def test_minimum_duration_enforced(self, renderer, tmp_srt):
        """Segments shorter than 0.5s must be extended."""
        segs = [{"index": 1, "start_sec": 1.0, "end_sec": 1.1, "text": "Hi"}]
        renderer.write_srt(segs, tmp_srt)
        content = open(tmp_srt).read()
        # end must be at least 1.5 (1.0 + 0.5)
        assert "00:00:01,500" in content

    def test_segments_numbered(self, renderer, tmp_srt):
        segs = [
            {"index": 1, "start_sec": 0.0, "end_sec": 1.0, "text": "One"},
            {"index": 2, "start_sec": 1.0, "end_sec": 2.0, "text": "Two"},
        ]
        renderer.write_srt(segs, tmp_srt)
        content = open(tmp_srt).read()
        assert content.strip().startswith("1")

    def test_empty_segments(self, renderer, tmp_srt):
        renderer.write_srt([], tmp_srt)
        assert open(tmp_srt).read() == ""

    def test_file_is_utf8(self, renderer, tmp_srt):
        segs = [{"index": 1, "start_sec": 0.0, "end_sec": 2.0, "text": "夏日清凉"}]
        renderer.write_srt(segs, tmp_srt)
        content = open(tmp_srt, encoding="utf-8").read()
        assert "夏日清凉" in content


# ── CaptionRenderer.write_ass ─────────────────────────────────────────────────

class TestWriteAss:
    @pytest.fixture()
    def renderer(self):
        return CaptionRenderer()

    @pytest.fixture()
    def tmp_ass(self, tmp_path):
        return str(tmp_path / "out.ass")

    def test_script_info_section(self, renderer, tmp_ass):
        renderer.write_ass([], tmp_ass)
        content = open(tmp_ass).read()
        assert "[Script Info]" in content

    def test_events_section(self, renderer, tmp_ass):
        renderer.write_ass([], tmp_ass)
        content = open(tmp_ass).read()
        assert "[Events]" in content

    def test_default_white_color(self, renderer, tmp_ass):
        renderer.write_ass([], tmp_ass)
        content = open(tmp_ass).read()
        assert "&H00FFFFFF" in content

    def test_custom_font_size(self, renderer, tmp_ass):
        renderer.write_ass([], tmp_ass, style={"font_size": 52})
        content = open(tmp_ass).read()
        assert ",52," in content

    def test_dialogue_line_written(self, renderer, tmp_ass):
        segs = [{"start_sec": 0.0, "end_sec": 2.0, "text": "Test caption"}]
        renderer.write_ass(segs, tmp_ass)
        content = open(tmp_ass).read()
        assert "Dialogue:" in content
        assert "Test caption" in content

    def test_newline_escaped(self, renderer, tmp_ass):
        segs = [{"start_sec": 0.0, "end_sec": 2.0, "text": "Line1\nLine2"}]
        renderer.write_ass(segs, tmp_ass)
        content = open(tmp_ass).read()
        assert "\\N" in content


# ── _ass_time ─────────────────────────────────────────────────────────────────

class TestAssTime:
    def test_zero(self):
        assert _ass_time(0.0) == "0:00:00.00"

    def test_one_second(self):
        assert _ass_time(1.0) == "0:00:01.00"

    def test_centiseconds(self):
        assert _ass_time(1.5) == "0:00:01.50"
