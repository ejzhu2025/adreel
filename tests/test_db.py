"""Tests for memory/db.py — Database CRUD operations.

Bug regression:
- project user_id="ej" (legacy mock) caused 0-credit lookup → false 402
  (covered in test_credits_and_execute.py; here we verify DB layer correctness)
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from memory.db import Database
from memory.schemas import BrandKit, ColorPalette, FontConfig, LogoConfig, SubtitleStyle, UserPrefs


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(db_path=tmp_path / "test.db")


# ── Project CRUD ──────────────────────────────────────────────────────────────

class TestProjectCRUD:
    def test_create_returns_uuid(self, db):
        pid = db.create_project(brief="Test brief", brand_id="brand1", user_id="u1")
        assert len(pid) == 8  # short UUID

    def test_create_project_defaults_to_pending(self, db):
        pid = db.create_project(brief="brief", brand_id="b", user_id="u")
        proj = db.get_project(pid)
        assert proj["status"] == "pending"

    def test_get_project_deserializes_plan_json(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u")
        plan = {"shot_list": [{"shot_id": "S1"}], "platform": "tiktok"}
        db.update_project_plan(pid, plan)
        proj = db.get_project(pid)
        # Must be a dict, not a raw JSON string — this was the bug causing 500 in tests
        assert isinstance(proj["latest_plan_json"], dict)
        assert proj["latest_plan_json"]["platform"] == "tiktok"

    def test_get_project_not_found_returns_none(self, db):
        assert db.get_project("nonexistent") is None

    def test_update_project_status(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u")
        db.update_project_status(pid, "running")
        assert db.get_project(pid)["status"] == "running"

    def test_update_project_output_appends_path(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u")
        db.update_project_output(pid, "/data/out1.mp4")
        db.update_project_output(pid, "/data/out2.mp4")
        proj = db.get_project(pid)
        assert "/data/out1.mp4" in proj["output_paths"]
        assert "/data/out2.mp4" in proj["output_paths"]
        assert proj["status"] == "done"

    def test_set_project_title(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u")
        db.set_project_title(pid, "Summer Vibes")
        assert db.get_project(pid)["title"] == "Summer Vibes"

    def test_set_project_title_idempotent(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u")
        db.set_project_title(pid, "Title One")
        db.set_project_title(pid, "Title Two")
        assert db.get_project(pid)["title"] == "Title Two"

    def test_delete_project(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u")
        db.delete_project(pid)
        assert db.get_project(pid) is None

    def test_list_projects(self, db):
        db.create_project(brief="A", brand_id="x", user_id="u")
        db.create_project(brief="B", brand_id="x", user_id="u")
        projects = db.list_projects()
        assert len(projects) >= 2

    def test_update_plan_preserves_other_fields(self, db):
        pid = db.create_project(brief="my brief", brand_id="x", user_id="u")
        plan = {"platform": "reels", "shot_list": []}
        db.update_project_plan(pid, plan)
        proj = db.get_project(pid)
        assert proj["brief"] == "my brief"
        assert proj["latest_plan_json"]["platform"] == "reels"

    def test_migrate_schema_idempotent(self, db):
        """Calling migration multiple times must not raise."""
        db._migrate_projects_schema()
        db._migrate_projects_schema()
        # title column must exist
        conn = sqlite3.connect(str(db.db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
        conn.close()
        assert "title" in cols


# ── BrandKit CRUD ─────────────────────────────────────────────────────────────

def _sample_kit(brand_id: str = "test") -> BrandKit:
    return BrandKit(
        brand_id=brand_id,
        name="Test Brand",
        colors=ColorPalette(primary="#FF0000", secondary="#00FF00", accent="#0000FF", background="#FFFFFF"),
        fonts=FontConfig(),
        logo=LogoConfig(),
        subtitle_style=SubtitleStyle(),
    )


class TestBrandKitCRUD:
    def test_upsert_and_get(self, db):
        kit = _sample_kit("brand1")
        db.upsert_brand_kit(kit)
        result = db.get_brand_kit("brand1")
        assert result is not None
        assert result.name == "Test Brand"

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_brand_kit("ghost") is None

    def test_upsert_overwrites(self, db):
        db.upsert_brand_kit(_sample_kit("b1"))
        kit2 = _sample_kit("b1")
        kit2.name = "Updated Name"
        db.upsert_brand_kit(kit2)
        assert db.get_brand_kit("b1").name == "Updated Name"

    def test_list_brand_kits(self, db):
        db.upsert_brand_kit(_sample_kit("k1"))
        db.upsert_brand_kit(_sample_kit("k2"))
        kits = db.list_brand_kits()
        ids = [k.brand_id for k in kits]
        assert "k1" in ids and "k2" in ids

    def test_delete_brand_kit(self, db):
        db.upsert_brand_kit(_sample_kit("del_me"))
        db.delete_brand_kit("del_me")
        assert db.get_brand_kit("del_me") is None


# ── Feedback ──────────────────────────────────────────────────────────────────

class TestFeedback:
    def test_add_feedback_v2_and_retrieve(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u1")
        db.add_feedback_v2(project_id=pid, user_id="u1", text="Loved it!", rating_overall=5, tags=["great"])
        rows = db.get_feedback(pid)
        assert len(rows) == 1
        assert rows[0]["text"] == "Loved it!"

    def test_has_feedback_true(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u1")
        db.add_feedback_v2(project_id=pid, user_id="u1", text="ok")
        assert db.has_feedback_for_project("u1", pid) is True

    def test_has_feedback_false(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u2")
        assert db.has_feedback_for_project("u2", pid) is False

    def test_has_feedback_wrong_user(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="u1")
        db.add_feedback_v2(project_id=pid, user_id="u1", text="ok")
        assert db.has_feedback_for_project("other_user", pid) is False

    def test_get_feedback_by_user(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="user_a")
        db.add_feedback_v2(project_id=pid, user_id="user_a", text="great")
        db.add_feedback_v2(project_id=pid, user_id="user_b", text="meh")
        rows = db.get_feedback_by_user("user_a")
        assert all(r["user_id"] == "user_a" for r in rows)

    def test_get_daily_feedback_credits_zero_if_none(self, db):
        assert db.get_daily_feedback_credits("nobody") == 0

    def test_get_daily_feedback_credits_sums_awards(self, db):
        pid = db.create_project(brief="b", brand_id="x", user_id="ux")
        fid1 = db.add_feedback_v2(project_id=pid, user_id="ux", text="a")
        fid2 = db.add_feedback_v2(project_id=pid, user_id="ux", text="b")
        db.update_feedback_review(fid1, 8, "good", 2)
        db.update_feedback_review(fid2, 9, "great", 3)
        total = db.get_daily_feedback_credits("ux")
        assert total == 5


# ── Feedback Categories ───────────────────────────────────────────────────────

class TestFeedbackCategories:
    def test_upsert_increments_frequency(self, db):
        db.upsert_feedback_category("Music quality", "User feedback about music")
        db.upsert_feedback_category("Music quality", "User feedback about music")
        cats = db.get_all_feedback_categories()
        music = next(c for c in cats if c["label"] == "Music quality")
        assert music["frequency"] == 2

    def test_get_active_returns_at_most_five(self, db):
        for i in range(8):
            for _ in range(i + 1):  # different frequencies
                db.upsert_feedback_category(f"Category {i}", f"desc {i}")
        active = db.get_active_feedback_categories()
        assert len(active) <= 5


# ── System Config ─────────────────────────────────────────────────────────────

class TestSystemConfig:
    def test_set_and_get(self, db):
        db.upsert_system_config("my_key", "my_value")
        assert db.get_system_config("my_key") == "my_value"

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_system_config("ghost_key") is None

    def test_overwrite(self, db):
        db.upsert_system_config("k", "v1")
        db.upsert_system_config("k", "v2")
        assert db.get_system_config("k") == "v2"

    def test_list_configs(self, db):
        db.upsert_system_config("a", "1")
        db.upsert_system_config("b", "2")
        configs = db.list_system_configs()
        assert "a" in configs and "b" in configs
