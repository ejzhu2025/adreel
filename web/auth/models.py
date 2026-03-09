"""auth/models.py — User table using the existing SQLite DB."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import agent.deps as deps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Ensure users table exists ─────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,          -- Google sub (stable user ID)
    email      TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    picture    TEXT NOT NULL DEFAULT '',
    credits    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fulfilled_sessions (
    session_id TEXT PRIMARY KEY,          -- Stripe checkout session ID
    user_id    TEXT NOT NULL,
    credits    INTEGER NOT NULL,
    fulfilled_at TEXT NOT NULL
);
"""


def ensure_schema() -> None:
    db = deps.db()
    with sqlite3.connect(str(db.db_path)) as conn:
        conn.executescript(_SCHEMA)


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class User:
    id: str
    email: str
    name: str
    picture: str
    credits: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "picture": self.picture,
            "credits": self.credits,
        }


# ── CRUD ──────────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    db = deps.db()
    conn = sqlite3.connect(str(db.db_path))
    conn.row_factory = sqlite3.Row
    return conn


def upsert_user(google_id: str, email: str, name: str, picture: str) -> User:
    """Insert new user or update name/picture if already exists. Returns User."""
    ensure_schema()
    now = _now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO users (id, email, name, picture, credits, created_at, updated_at)
               VALUES (?, ?, ?, ?, 10, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,
                   picture=excluded.picture,
                   updated_at=excluded.updated_at""",
            (google_id, email, name, picture, now, now),
        )
    return get_user(google_id)  # type: ignore[return-value]


def get_user(user_id: str) -> Optional[User]:
    ensure_schema()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return User(**dict(row))


def get_user_by_email(email: str) -> Optional[User]:
    ensure_schema()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
    if row is None:
        return None
    return User(**dict(row))
