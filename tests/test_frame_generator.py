"""Tests for render/frame_generator.py — PIL placeholder frame creation."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from render.frame_generator import FrameGenerator, _TYPE_GRADIENTS


BRAND_KIT = {
    "colors": {"primary": "#6C5CE7", "secondary": "#A29BFE", "background": "#1A1A2E", "text": "#FFFFFF"},
    "subtitle_style": {"font_size": 38, "primary_color": "#FFFFFF"},
}


@pytest.fixture()
def gen(tmp_path):
    return FrameGenerator(brand_kit=BRAND_KIT, work_dir=tmp_path)


# ── generate_frame ────────────────────────────────────────────────────────────

class TestGenerateFrame:
    def test_returns_path_object(self, gen):
        path = gen.generate_frame(shot_id="S1", shot_type="product", text_overlay="Hello")
        assert isinstance(path, Path)
        assert path.exists()

    def test_output_is_valid_1080x1920_image(self, gen):
        path = gen.generate_frame(shot_id="S1", shot_type="product")
        img = Image.open(path)
        assert img.size == (1080, 1920)

    def test_all_shot_types_work(self, gen):
        for shot_type in _TYPE_GRADIENTS:
            path = gen.generate_frame(shot_id=shot_type, shot_type=shot_type)
            assert path.exists(), f"Failed for shot_type={shot_type}"

    def test_unknown_shot_type_does_not_crash(self, gen):
        path = gen.generate_frame(shot_id="X", shot_type="unknown_type")
        assert path.exists()

    def test_output_file_non_trivial_size(self, gen):
        path = gen.generate_frame(shot_id="S1", shot_type="lifestyle", text_overlay="Summer Vibes")
        assert path.stat().st_size > 5_000  # at least 5KB

    def test_frame_saved_with_shot_id_in_name(self, gen):
        path = gen.generate_frame(shot_id="MyShotXYZ", shot_type="wide")
        assert "MyShotXYZ" in path.name

    def test_text_overlay_included(self, gen):
        """Frame with text overlay must produce a file (visual test only — just check it doesn't crash)."""
        path = gen.generate_frame(shot_id="T1", shot_type="text", text_overlay="BUY NOW")
        assert path.exists()

    def test_is_outro_flag_does_not_crash(self, gen):
        path = gen.generate_frame(shot_id="outro", shot_type="product", is_outro=True)
        assert path.exists()


# ── Font cache ────────────────────────────────────────────────────────────────

class TestFontCache:
    def test_font_cache_reuses_object(self, gen):
        f1 = gen._get_font(32)
        f2 = gen._get_font(32)
        assert f1 is f2

    def test_different_sizes_different_objects(self, gen):
        f1 = gen._get_font(24)
        f2 = gen._get_font(48)
        assert f1 is not f2

    def test_fallback_font_does_not_crash(self, gen):
        """Even if no TrueType font found, must not raise (uses ImageFont.load_default)."""
        font = gen._get_font(20)
        assert font is not None
