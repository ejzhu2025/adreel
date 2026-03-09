"""Tests for web/auth/deps.py + web/auth/models.py — JWT and user auth.

Bug regressions:
- New user default credits must be 10 (not 0) so they can try turbo once
- upsert_user ON CONFLICT must NOT reset credits for existing users
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from jose import jwt

from web.auth.deps import (
    JWT_ALGORITHM,
    JWT_SECRET,
    create_token,
    decode_token,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def user_db(tmp_path):
    """Isolated SQLite DB with users table only."""
    db_path = tmp_path / "auth_test.db"
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
    conn.close()
    return db_path


# ── JWT ───────────────────────────────────────────────────────────────────────

class TestJWT:
    def test_create_and_decode(self):
        token = create_token("user123")
        user_id = decode_token(token)
        assert user_id == "user123"

    def test_expired_token_returns_none(self):
        # Manually create an already-expired token
        payload = {
            "sub": "user_x",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        assert decode_token(token) is None

    def test_tampered_signature_returns_none(self):
        token = create_token("legit_user")
        tampered = token[:-4] + "XXXX"
        assert decode_token(tampered) is None

    def test_empty_string_returns_none(self):
        assert decode_token("") is None

    def test_garbage_returns_none(self):
        assert decode_token("not.a.jwt") is None

    def test_token_contains_correct_sub(self):
        token = create_token("my_user_id")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["sub"] == "my_user_id"

    def test_token_expires_in_30_days(self):
        before = datetime.now(timezone.utc)
        token = create_token("u")
        after = datetime.now(timezone.utc)
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        # exp should be ~30 days from now (allow 5s slack)
        assert (exp - before).days >= 29
        assert (exp - after).days <= 31


# ── User model ────────────────────────────────────────────────────────────────

class TestUserModel:
    def _make_deps(self, db_path):
        import agent.deps as deps
        class FakeDB:
            db_path = db_path  # used by ensure_schema / _conn
        return FakeDB()

    def test_new_user_gets_10_credits(self, user_db):
        """Bug regression: new users must start with 10 credits, not 0."""
        import agent.deps as deps
        from web.auth.models import upsert_user

        class FakeDB:
            pass
        FakeDB.db_path = user_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            user = upsert_user("google_001", "alice@example.com", "Alice", "")
        assert user.credits == 10

    def test_existing_user_credits_not_reset(self, user_db):
        """Bug regression: upsert must preserve credits for existing users."""
        import agent.deps as deps
        from web.auth.models import upsert_user

        class FakeDB:
            pass
        FakeDB.db_path = user_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            upsert_user("google_002", "bob@example.com", "Bob", "")
            # Manually top up credits
            conn = sqlite3.connect(str(user_db))
            conn.execute("UPDATE users SET credits=50 WHERE id='google_002'")
            conn.commit()
            conn.close()
            # Upsert again (e.g. user logs back in)
            user = upsert_user("google_002", "bob@example.com", "Bob Updated", "new_pic")

        assert user.credits == 50, "Credits must NOT be reset on re-login"

    def test_upsert_updates_name_and_picture(self, user_db):
        import agent.deps as deps
        from web.auth.models import upsert_user

        class FakeDB:
            pass
        FakeDB.db_path = user_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            upsert_user("google_003", "carol@example.com", "Carol Old", "old_pic")
            user = upsert_user("google_003", "carol@example.com", "Carol New", "new_pic")

        assert user.name == "Carol New"
        assert user.picture == "new_pic"

    def test_get_user_not_found_returns_none(self, user_db):
        import agent.deps as deps
        from web.auth.models import get_user

        class FakeDB:
            pass
        FakeDB.db_path = user_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            result = get_user("ghost_id")
        assert result is None

    def test_get_user_by_email(self, user_db):
        import agent.deps as deps
        from web.auth.models import upsert_user, get_user_by_email

        class FakeDB:
            pass
        FakeDB.db_path = user_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            upsert_user("google_004", "dave@example.com", "Dave", "")
            user = get_user_by_email("dave@example.com")
        assert user is not None
        assert user.id == "google_004"

    def test_get_user_by_email_not_found(self, user_db):
        import agent.deps as deps
        from web.auth.models import get_user_by_email

        class FakeDB:
            pass
        FakeDB.db_path = user_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            assert get_user_by_email("nobody@example.com") is None

    def test_different_users_independent_credits(self, user_db):
        import agent.deps as deps
        from web.auth.models import upsert_user

        class FakeDB:
            pass
        FakeDB.db_path = user_db

        with patch.object(deps, 'db', return_value=FakeDB()):
            u1 = upsert_user("g1", "u1@x.com", "U1", "")
            u2 = upsert_user("g2", "u2@x.com", "U2", "")
        assert u1.credits == 10
        assert u2.credits == 10


# ── optional_user dependency ──────────────────────────────────────────────────

class TestOptionalUser:
    def test_no_cookie_returns_none(self):
        from web.auth.deps import optional_user
        # Call with no cookie
        result = optional_user(vah_session=None)
        assert result is None

    def test_invalid_token_returns_none(self):
        from web.auth.deps import optional_user
        result = optional_user(vah_session="invalid.token.here")
        assert result is None
