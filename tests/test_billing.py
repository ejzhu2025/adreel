"""Tests for web/billing/credits.py — idempotency, add, deduct, get, cost_for_plan."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path):
    """SQLite DB with users + fulfilled_sessions tables, pre-seeded with one user."""
    db_path = tmp_path / "billing_test.db"
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
    """)
    conn.execute(
        "INSERT INTO users (id, email, name, picture, credits, created_at, updated_at) "
        "VALUES ('u1', 'user@test.com', 'User', '', 50, '2025-01-01', '2025-01-01')"
    )
    conn.commit()
    conn.close()
    return db_path


class FakeDB:
    def __init__(self, db_path):
        self.db_path = db_path


def _patch_db(tmp_db):
    """Context manager that patches deps.db() and ensure_schema."""
    import agent.deps as deps
    fake = FakeDB(tmp_db)
    return (
        patch.object(deps, 'db', return_value=fake),
        patch('web.auth.models.ensure_schema'),
    )


# ── get_credits ───────────────────────────────────────────────────────────────

class TestGetCredits:
    def test_existing_user_returns_balance(self, tmp_db):
        from web.billing.credits import get_credits
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            assert get_credits('u1') == 50

    def test_unknown_user_returns_zero(self, tmp_db):
        from web.billing.credits import get_credits
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            assert get_credits('nonexistent') == 0


# ── add_credits ───────────────────────────────────────────────────────────────

class TestAddCredits:
    def test_adds_to_existing_balance(self, tmp_db):
        from web.billing.credits import add_credits
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            new_bal = add_credits('u1', 20)
        assert new_bal == 70

    def test_multiple_adds_accumulate(self, tmp_db):
        from web.billing.credits import add_credits
        import agent.deps as deps
        fake = FakeDB(tmp_db)
        with patch.object(deps, 'db', return_value=fake), \
             patch('web.auth.models.ensure_schema'):
            add_credits('u1', 10)
            bal = add_credits('u1', 5)
        assert bal == 65  # 50 + 10 + 5


# ── deduct_credits ────────────────────────────────────────────────────────────

class TestDeductCredits:
    def test_deduct_success(self, tmp_db):
        from web.billing.credits import deduct_credits
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            new_bal = deduct_credits('u1', 10)
        assert new_bal == 40

    def test_deduct_all(self, tmp_db):
        from web.billing.credits import deduct_credits
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            new_bal = deduct_credits('u1', 50)
        assert new_bal == 0

    def test_deduct_insufficient_raises(self, tmp_db):
        from web.billing.credits import deduct_credits
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            with pytest.raises(ValueError, match="Insufficient"):
                deduct_credits('u1', 200)

    def test_deduct_unknown_user_raises(self, tmp_db):
        """Unknown user has 0 credits — any deduction fails."""
        from web.billing.credits import deduct_credits
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            with pytest.raises(ValueError):
                deduct_credits('nobody', 1)


# ── fulfill_session (idempotency) ─────────────────────────────────────────────

class TestFulfillSession:
    def test_first_fulfill_adds_credits(self, tmp_db):
        from web.billing.credits import fulfill_session
        import agent.deps as deps
        with patch.object(deps, 'db', return_value=FakeDB(tmp_db)), \
             patch('web.auth.models.ensure_schema'):
            was_new, bal = fulfill_session('sess_001', 'u1', 30)
        assert was_new is True
        assert bal == 80  # 50 + 30

    def test_second_fulfill_is_noop(self, tmp_db):
        """Calling fulfill_session twice with same session_id only adds credits once."""
        from web.billing.credits import fulfill_session
        import agent.deps as deps
        fake = FakeDB(tmp_db)
        with patch.object(deps, 'db', return_value=fake), \
             patch('web.auth.models.ensure_schema'):
            was_new1, bal1 = fulfill_session('sess_002', 'u1', 25)
            was_new2, bal2 = fulfill_session('sess_002', 'u1', 25)
        assert was_new1 is True
        assert was_new2 is False
        assert bal1 == 75   # 50 + 25
        assert bal2 == 75   # unchanged — same session

    def test_different_sessions_both_add(self, tmp_db):
        """Two different session IDs both add credits."""
        from web.billing.credits import fulfill_session
        import agent.deps as deps
        fake = FakeDB(tmp_db)
        with patch.object(deps, 'db', return_value=fake), \
             patch('web.auth.models.ensure_schema'):
            _, bal1 = fulfill_session('sess_A', 'u1', 10)
            _, bal2 = fulfill_session('sess_B', 'u1', 10)
        assert bal1 == 60
        assert bal2 == 70

    def test_idempotency_triple_call(self, tmp_db):
        """3 identical calls → credits added only once."""
        from web.billing.credits import fulfill_session
        import agent.deps as deps
        fake = FakeDB(tmp_db)
        with patch.object(deps, 'db', return_value=fake), \
             patch('web.auth.models.ensure_schema'):
            r1 = fulfill_session('sess_triple', 'u1', 100)
            r2 = fulfill_session('sess_triple', 'u1', 100)
            r3 = fulfill_session('sess_triple', 'u1', 100)
        assert r1 == (True, 150)
        assert r2 == (False, 150)
        assert r3 == (False, 150)


# ── cost_for_plan ─────────────────────────────────────────────────────────────

class TestCostForPlan:
    def test_turbo_cost(self):
        from web.billing.credits import cost_for_plan
        assert cost_for_plan(5, "turbo") == 5
        assert cost_for_plan(0, "turbo") == 0
        assert cost_for_plan(1, "turbo") == 1

    def test_hd_cost(self):
        from web.billing.credits import cost_for_plan
        assert cost_for_plan(5, "hd") == 15
        assert cost_for_plan(1, "hd") == 3

    def test_unknown_quality_defaults_to_turbo(self):
        from web.billing.credits import cost_for_plan
        assert cost_for_plan(4, "unknown") == 4  # turbo rate
