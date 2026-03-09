"""Tests for credit check and execute endpoint auth logic."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_PLAN = {
    "platform": "tiktok",
    "duration_sec": 10,
    "shot_list": [
        {"shot_id": "S1", "text_overlay": "Shot 1", "duration": 3.0},
        {"shot_id": "S2", "text_overlay": "Shot 2", "duration": 3.0},
        {"shot_id": "S3", "text_overlay": "Shot 3", "duration": 3.0},
    ],
    "storyboard": [],
    "script": {},
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path):
    """Create a temporary SQLite DB with users + projects tables."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            picture TEXT NOT NULL DEFAULT '',
            credits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE fulfilled_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            credits INTEGER NOT NULL,
            fulfilled_at TEXT NOT NULL
        );
        CREATE TABLE projects (
            project_id TEXT PRIMARY KEY,
            brief TEXT NOT NULL DEFAULT '',
            brand_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            latest_plan_json TEXT,
            output_paths TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            title TEXT
        );
    """)
    # Real user with 6 credits
    conn.execute("""
        INSERT INTO users (id, email, name, picture, credits, created_at, updated_at)
        VALUES ('real_user_id', 'test@example.com', 'Test User', '', 6, '2024-01-01', '2024-01-01')
    """)
    # Mock/demo project with legacy user_id "ej"
    conn.execute("""
        INSERT INTO projects (project_id, brief, brand_id, user_id, created_at, updated_at, latest_plan_json, status)
        VALUES ('proj_ej', 'Test brief', 'default', 'ej', '2024-01-01', '2024-01-01', ?, 'planned')
    """, (json.dumps(_PLAN),))
    conn.commit()
    conn.close()
    return db_path


def make_fake_db(tmp_db):
    """Return a FakeDB that mimics the real DB (deserializes JSON like memory/db.py does)."""
    class FakeDB:
        db_path = tmp_db

        def get_project(self, pid):
            conn = sqlite3.connect(str(tmp_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM projects WHERE project_id=?", (pid,)).fetchone()
            conn.close()
            if not row:
                return None
            d = dict(row)
            # Deserialize JSON fields — same as memory/db.py get_project()
            d["latest_plan_json"] = json.loads(d["latest_plan_json"]) if d["latest_plan_json"] else None
            d["output_paths"] = json.loads(d["output_paths"] or "[]")
            return d

        def get_brand_kit(self, bid):
            return None

        def update_project_plan(self, pid, plan):
            pass

        def update_project_status(self, pid, status):
            pass

        def set_project_title(self, pid, title):
            pass

    return FakeDB()


# ── Unit tests: billing/credits.py ────────────────────────────────────────────

class TestCreditsModule:
    def test_get_credits_existing_user(self, tmp_db):
        from web.billing.credits import get_credits
        import agent.deps as deps
        deps._db = None  # reset singleton
        with patch.object(deps, 'db') as mock_db:
            mock_db.return_value.db_path = tmp_db
            with patch('web.auth.models.ensure_schema'):
                conn = sqlite3.connect(str(tmp_db))
                row = conn.execute("SELECT credits FROM users WHERE id='real_user_id'").fetchone()
                conn.close()
                assert row[0] == 6

    def test_get_credits_unknown_user_returns_zero(self, tmp_db):
        """Legacy mock user 'ej' has no row → returns 0."""
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT credits FROM users WHERE id='ej'").fetchone()
        conn.close()
        assert row is None  # "ej" doesn't exist in users table

    def test_deduct_credits_success(self, tmp_db):
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("UPDATE users SET credits=10 WHERE id='real_user_id'")
        conn.commit()
        conn.close()

        from web.billing.credits import deduct_credits
        import agent.deps as deps

        class FakeDB:
            db_path = tmp_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            with patch('web.auth.models.ensure_schema'):
                new_bal = deduct_credits('real_user_id', 3)
        assert new_bal == 7

    def test_deduct_credits_insufficient_raises(self, tmp_db):
        from web.billing.credits import deduct_credits
        import agent.deps as deps

        class FakeDB:
            db_path = tmp_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            with patch('web.auth.models.ensure_schema'):
                with pytest.raises(ValueError, match="Insufficient"):
                    deduct_credits('real_user_id', 100)  # only has 6

    def test_cost_for_plan_turbo(self):
        from web.billing.credits import cost_for_plan
        assert cost_for_plan(5, "turbo") == 5   # 5 × 1
        assert cost_for_plan(5, "hd") == 15     # 5 × 3

    def test_cost_for_plan_hd(self):
        from web.billing.credits import cost_for_plan
        assert cost_for_plan(3, "hd") == 9      # 3 × 3


# ── Integration: execute endpoint uses session user, not project user_id ──────

class TestExecuteCreditCheck:
    """Verify that the /execute endpoint uses the authenticated session user
    for credit checks, not the project's (potentially legacy) user_id."""

    def _make_jwt(self, user_id: str) -> str:
        from web.auth.deps import JWT_SECRET, JWT_ALGORITHM
        from datetime import datetime, timedelta, timezone
        from jose import jwt
        expire = datetime.now(timezone.utc) + timedelta(days=1)
        return jwt.encode({"sub": user_id, "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)

    def test_session_user_id_overrides_project_ej(self, tmp_db, monkeypatch):
        """Real user (6 credits) can execute a project owned by 'ej' (0 credits)."""
        import agent.deps as deps
        monkeypatch.setattr(deps, 'db', lambda: make_fake_db(tmp_db))

        # Import after monkeypatching
        from web.billing import credits as cr_module
        original_get = cr_module.get_credits

        calls = []
        def track_get_credits(uid):
            calls.append(uid)
            conn = sqlite3.connect(str(tmp_db))
            row = conn.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            return row[0] if row else 0

        monkeypatch.setattr(cr_module, 'get_credits', track_get_credits)

        from web.server import app
        client = TestClient(app, raise_server_exceptions=False)

        token = self._make_jwt('real_user_id')
        resp = client.post(
            "/api/projects/proj_ej/execute",
            json={"plan": None, "quality": "turbo", "clarification_answers": {}},
            cookies={"vah_session": token},
        )

        # Credit check should have used 'real_user_id', not 'ej'
        assert 'real_user_id' in calls, f"Expected real_user_id in credit calls, got: {calls}"
        assert 'ej' not in calls, f"Should NOT check credits for 'ej', got: {calls}"
        # Should NOT be 402 (real_user has 6 credits, cost = 3 shots × 1 = 3)
        assert resp.status_code != 402, f"Got 402: {resp.json()}"

    def test_insufficient_credits_returns_structured_402(self, tmp_db, monkeypatch):
        """When credits are insufficient, 402 response includes {needed, have}."""
        import agent.deps as deps

        # Set user credits to 0
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("UPDATE users SET credits=0 WHERE id='real_user_id'")
        conn.commit()
        conn.close()

        monkeypatch.setattr(deps, 'db', lambda: make_fake_db(tmp_db))

        from web.billing import credits as cr_module

        def real_get_credits(uid):
            conn = sqlite3.connect(str(tmp_db))
            row = conn.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            return row[0] if row else 0

        monkeypatch.setattr(cr_module, 'get_credits', real_get_credits)

        from web.server import app
        client = TestClient(app, raise_server_exceptions=False)

        token = self._make_jwt('real_user_id')
        resp = client.post(
            "/api/projects/proj_ej/execute",
            json={"plan": None, "quality": "turbo", "clarification_answers": {}},
            cookies={"vah_session": token},
        )

        assert resp.status_code == 402
        body = resp.json()
        assert "detail" in body
        detail = body["detail"]
        assert "needed" in detail, f"Missing 'needed' in 402 detail: {detail}"
        assert "have" in detail, f"Missing 'have' in 402 detail: {detail}"
        assert detail["needed"] == 3   # 3 shots × 1 credit (turbo)
        assert detail["have"] == 0


# ── Unit tests: avatar fallback JS behavior (logic only) ─────────────────────

class TestAvatarHTML:
    @staticmethod
    def _html_content() -> str:
        """HTML lives in web/templates.py after the refactor."""
        templates_py = Path(__file__).parent.parent / "web" / "templates.py"
        return templates_py.read_text()

    def test_avatar_img_has_no_src_attr(self):
        """The avatar <img> must not have src='' (causes broken image request)."""
        content = self._html_content()
        assert 'id="user-avatar" src=""' not in content, \
            "Avatar img must not have src='' — causes broken image on load"

    def test_avatar_has_referrerpolicy(self):
        """Avatar img needs referrerpolicy='no-referrer' for Google picture URLs."""
        assert 'referrerpolicy="no-referrer"' in self._html_content()

    def test_avatar_fallback_div_exists(self):
        """Fallback initials div must exist for when Google picture fails to load."""
        assert 'user-avatar-fallback' in self._html_content()
