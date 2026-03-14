"""SQLite long-term memory store — with optional Turso cloud sync."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from memory.schemas import BrandKit, UserPrefs, ProjectRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TURSO_URL   = "".join(os.getenv("TURSO_DATABASE_URL", "").split())
_TURSO_TOKEN = "".join(os.getenv("TURSO_AUTH_TOKEN", "").split())
_USE_TURSO   = bool(_TURSO_URL and _TURSO_TOKEN)


class _SyncConn:
    """Wraps a libsql connection with sqlite3-compatible context manager.
    Calls conn.sync() on open (pull) and after commit (push)."""

    def __init__(self, raw):
        self._c = raw
        self._c.sync()           # pull latest from Turso

    # ── context manager ───────────────────────────────────────────────────────
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._c.commit()
            self._c.sync()       # push writes to Turso
        else:
            try:
                self._c.rollback()
            except Exception:
                pass
        return False

    # ── proxy the methods used by Database ───────────────────────────────────
    def execute(self, sql, params=()):
        return self._c.execute(sql, params)

    def executescript(self, sql: str):
        """libsql lacks executescript — split on ';' and execute each statement."""
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                try:
                    self._c.execute(stmt)
                except Exception:
                    pass
        self._c.commit()
        self._c.sync()

    @property
    def row_factory(self):
        return self._c.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._c.row_factory = value


class Database:
    def __init__(self, db_path: str | Path = "./data/vah.db"):
        self.db_path = Path(db_path)
        if _USE_TURSO:
            # Use /tmp as local replica cache (ephemeral OK — remote is source of truth)
            self.db_path = Path("/tmp/vah_replica.db")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self):
        if _USE_TURSO:
            import libsql_experimental as libsql  # pip: libsql-experimental
            raw = libsql.connect(
                str(self.db_path),
                sync_url=_TURSO_URL,
                auth_token=_TURSO_TOKEN,
            )
            raw.row_factory = sqlite3.Row
            return _SyncConn(raw)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS brand_kits (
            brand_id   TEXT PRIMARY KEY,
            json       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id    TEXT PRIMARY KEY,
            json       TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
            project_id       TEXT PRIMARY KEY,
            brief            TEXT NOT NULL,
            brand_id         TEXT NOT NULL DEFAULT 'default',
            user_id          TEXT NOT NULL DEFAULT 'default',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            latest_plan_json TEXT,
            output_paths     TEXT NOT NULL DEFAULT '[]',
            status           TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS assets (
            asset_id   TEXT PRIMARY KEY,
            brand_id   TEXT NOT NULL,
            type       TEXT NOT NULL,
            path       TEXT NOT NULL,
            metadata   TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id       TEXT NOT NULL,
            text             TEXT NOT NULL DEFAULT '',
            rating           INTEGER,
            -- v2 fields (added via migration below)
            user_id          TEXT NOT NULL DEFAULT '',
            user_name        TEXT NOT NULL DEFAULT '',
            rating_overall   INTEGER,
            tags             TEXT NOT NULL DEFAULT '[]',
            credits_spent    INTEGER NOT NULL DEFAULT 0,
            review_status    TEXT NOT NULL DEFAULT 'pending',
            review_score     INTEGER,
            review_reasoning TEXT,
            credits_awarded  INTEGER NOT NULL DEFAULT 0,
            reviewed_at      TEXT,
            analysis_batch_id TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback_categories (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            label          TEXT NOT NULL UNIQUE,
            description    TEXT NOT NULL DEFAULT '',
            frequency      INTEGER NOT NULL DEFAULT 0,
            first_seen_at  TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            is_active      INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS feedback_analysis (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id       TEXT UNIQUE NOT NULL,
            analysis_date  TEXT NOT NULL,
            feedback_count INTEGER NOT NULL DEFAULT 0,
            report_json    TEXT NOT NULL DEFAULT '{}',
            fix_status     TEXT NOT NULL DEFAULT 'pending',
            fixes_json     TEXT NOT NULL DEFAULT '[]',
            created_at     TEXT NOT NULL,
            completed_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS feedback_fix_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id    TEXT NOT NULL,
            fix_type    TEXT NOT NULL DEFAULT 'config_change',
            target_key  TEXT NOT NULL,
            old_value   TEXT,
            new_value   TEXT,
            applied     INTEGER NOT NULL DEFAULT 0,
            applied_at  TEXT,
            notes       TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_config (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT 'system'
        );
        """
        with self._conn() as conn:
            conn.executescript(ddl)
        self._migrate_feedback_schema()
        self._migrate_projects_schema()

    def _migrate_projects_schema(self) -> None:
        """Add title column to projects table (idempotent)."""
        with self._conn() as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
            if "title" not in existing:
                conn.execute("ALTER TABLE projects ADD COLUMN title TEXT")

    def _migrate_feedback_schema(self) -> None:
        """Add v2 columns to feedback table for existing DBs (idempotent)."""
        new_cols = {
            "user_id": "TEXT NOT NULL DEFAULT ''",
            "user_name": "TEXT NOT NULL DEFAULT ''",
            "rating_overall": "INTEGER",
            "tags": "TEXT NOT NULL DEFAULT '[]'",
            "credits_spent": "INTEGER NOT NULL DEFAULT 0",
            "review_status": "TEXT NOT NULL DEFAULT 'pending'",
            "review_score": "INTEGER",
            "review_reasoning": "TEXT",
            "credits_awarded": "INTEGER NOT NULL DEFAULT 0",
            "reviewed_at": "TEXT",
            "analysis_batch_id": "TEXT",
        }
        with self._conn() as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(feedback)")}
            for col, typedef in new_cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} {typedef}")

    # ── Brand kits ────────────────────────────────────────────────────────────

    def upsert_brand_kit(self, kit: BrandKit) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO brand_kits (brand_id, json, updated_at) VALUES (?,?,?)",
                (kit.brand_id, kit.model_dump_json(), _now()),
            )

    def get_brand_kit(self, brand_id: str) -> Optional[BrandKit]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM brand_kits WHERE brand_id=?", (brand_id,)
            ).fetchone()
        if row is None:
            return None
        return BrandKit.model_validate_json(row["json"])

    def list_brand_kits(self) -> list[BrandKit]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT json FROM brand_kits ORDER BY updated_at DESC"
            ).fetchall()
        return [BrandKit.model_validate_json(row["json"]) for row in rows]

    def delete_brand_kit(self, brand_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM brand_kits WHERE brand_id=?", (brand_id,))

    # ── User prefs ────────────────────────────────────────────────────────────

    def upsert_user_prefs(self, prefs: UserPrefs) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_prefs (user_id, json, updated_at) VALUES (?,?,?)",
                (prefs.user_id, prefs.model_dump_json(), _now()),
            )

    def get_user_prefs(self, user_id: str) -> Optional[UserPrefs]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM user_prefs WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return UserPrefs.model_validate_json(row["json"])

    # ── Projects ──────────────────────────────────────────────────────────────

    def create_project(
        self,
        brief: str,
        brand_id: str = "default",
        user_id: str = "default",
        project_id: Optional[str] = None,
    ) -> str:
        pid = project_id or str(uuid.uuid4())[:8]
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO projects
                   (project_id,brief,brand_id,user_id,created_at,updated_at,status)
                   VALUES (?,?,?,?,?,?,'pending')""",
                (pid, brief, brand_id, user_id, now, now),
            )
        return pid

    def get_project(self, project_id: str) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["output_paths"] = json.loads(d["output_paths"] or "[]")
        d["latest_plan_json"] = json.loads(d["latest_plan_json"]) if d["latest_plan_json"] else None
        return d

    def list_projects(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["output_paths"] = json.loads(d["output_paths"] or "[]")
            d["latest_plan_json"] = json.loads(d["latest_plan_json"]) if d["latest_plan_json"] else None
            result.append(d)
        return result

    def update_project_plan(self, project_id: str, plan: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET latest_plan_json=?, updated_at=? WHERE project_id=?",
                (json.dumps(plan), _now(), project_id),
            )

    def update_project_output(self, project_id: str, output_path: str, status: str = "done") -> None:
        project = self.get_project(project_id)
        paths = project["output_paths"] if project else []
        if output_path not in paths:
            paths.append(output_path)
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET output_paths=?, status=?, updated_at=? WHERE project_id=?",
                (json.dumps(paths), status, _now(), project_id),
            )

    def update_project_status(self, project_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET status=?, updated_at=? WHERE project_id=?",
                (status, _now(), project_id),
            )

    def delete_project(self, project_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM projects WHERE project_id=?", (project_id,))

    def set_project_title(self, project_id: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET title=? WHERE project_id=?",
                (title, project_id),
            )

    # ── Assets ────────────────────────────────────────────────────────────────

    def upsert_asset(
        self,
        brand_id: str,
        asset_type: str,
        path: str,
        metadata: dict | None = None,
        asset_id: Optional[str] = None,
    ) -> str:
        aid = asset_id or str(uuid.uuid4())[:8]
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO assets (asset_id,brand_id,type,path,metadata) VALUES (?,?,?,?,?)",
                (aid, brand_id, asset_type, path, json.dumps(metadata or {})),
            )
        return aid

    def get_assets(self, brand_id: str, asset_type: Optional[str] = None) -> list[dict]:
        with self._conn() as conn:
            if asset_type:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE brand_id=? AND type=?", (brand_id, asset_type)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE brand_id=?", (brand_id,)
                ).fetchall()
        return [dict(r) for r in rows]

    # ── Feedback ──────────────────────────────────────────────────────────────

    def add_feedback(self, project_id: str, text: str, rating: Optional[int] = None) -> None:
        """Legacy minimal insert (kept for backward compat)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO feedback (project_id,text,rating,created_at) VALUES (?,?,?,?)",
                (project_id, text, rating, _now()),
            )

    def add_feedback_v2(
        self,
        project_id: str,
        user_id: str = "",
        user_name: str = "",
        rating_overall: Optional[int] = None,
        tags: list | None = None,
        text: str = "",
        credits_spent: int = 0,
    ) -> int:
        """Full v2 feedback insert. Returns new row id."""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO feedback
                   (project_id, user_id, user_name, rating_overall, tags, text,
                    credits_spent, review_status, created_at)
                   VALUES (?,?,?,?,?,?,?,'pending',?)""",
                (project_id, user_id, user_name, rating_overall,
                 json.dumps(tags or []), text, credits_spent, _now()),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_feedback(self, project_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_feedback_by_id(self, feedback_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT f.*, p.brief FROM feedback f
                   LEFT JOIN projects p ON p.project_id = f.project_id
                   WHERE f.id=?""",
                (feedback_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_feedback_by_user(self, user_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT f.*, p.brief,
                   fl.notes as fix_notes, fl.applied as fix_applied, fl.applied_at
                   FROM feedback f
                   LEFT JOIN projects p ON p.project_id = f.project_id
                   LEFT JOIN feedback_fix_log fl ON fl.batch_id = f.analysis_batch_id
                       AND fl.applied = 1
                   WHERE f.user_id=?
                   ORDER BY f.created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_feedback_for_project(self, user_id: str, project_id: str) -> bool:
        """Return True if this user has any feedback (any status) for this project."""
        if not user_id:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM feedback WHERE user_id=? AND project_id=? LIMIT 1",
                (user_id, project_id),
            ).fetchone()
        return row is not None

    def get_recent_feedback(self, user_id: str, project_id: str, minutes: int = 10) -> list[dict]:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM feedback WHERE user_id=? AND project_id=? AND created_at>=?",
                (user_id, project_id, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_feedback_review(
        self,
        feedback_id: int,
        score: int,
        reasoning: str,
        credits: int,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE feedback SET review_status='reviewed', review_score=?,
                   review_reasoning=?, credits_awarded=?, reviewed_at=? WHERE id=?""",
                (score, reasoning, credits, _now(), feedback_id),
            )

    def get_daily_feedback_credits(self, user_id: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(credits_awarded),0) FROM feedback "
                "WHERE user_id=? AND DATE(created_at)=?",
                (user_id, today),
            ).fetchone()
        return row[0] if row else 0

    def get_feedback_for_analysis(self, since: str) -> list[dict]:
        """Return reviewed feedback (score>=20) since given ISO timestamp."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT f.*, p.brief FROM feedback f
                   LEFT JOIN projects p ON p.project_id = f.project_id
                   WHERE f.review_status='reviewed' AND f.review_score >= 20
                   AND f.created_at >= ? AND f.analysis_batch_id IS NULL
                   ORDER BY f.created_at DESC LIMIT 200""",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_feedback_analyzed(self, feedback_ids: list[int], batch_id: str) -> None:
        if not feedback_ids:
            return
        placeholders = ",".join("?" * len(feedback_ids))
        with self._conn() as conn:
            conn.execute(
                f"UPDATE feedback SET analysis_batch_id=? WHERE id IN ({placeholders})",
                [batch_id, *feedback_ids],
            )

    # ── Feedback categories ────────────────────────────────────────────────────

    def upsert_feedback_category(self, label: str, description: str = "") -> None:
        now = _now()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id, frequency FROM feedback_categories WHERE label=?", (label,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE feedback_categories SET frequency=frequency+1, last_active_at=?, "
                    "description=CASE WHEN description='' THEN ? ELSE description END WHERE label=?",
                    (now, description, label),
                )
            else:
                conn.execute(
                    "INSERT INTO feedback_categories (label,description,frequency,first_seen_at,last_active_at,is_active) "
                    "VALUES (?,?,1,?,?,1)",
                    (label, description, now, now),
                )
            # Keep only top 5 active (by frequency)
            conn.execute(
                "UPDATE feedback_categories SET is_active=0"
            )
            conn.execute(
                "UPDATE feedback_categories SET is_active=1 WHERE id IN "
                "(SELECT id FROM feedback_categories ORDER BY frequency DESC LIMIT 5)"
            )

    def get_active_feedback_categories(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT label, description, frequency FROM feedback_categories "
                "WHERE is_active=1 ORDER BY frequency DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_feedback_categories(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT label, description, frequency FROM feedback_categories ORDER BY frequency DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Feedback analysis ──────────────────────────────────────────────────────

    def save_analysis(
        self,
        batch_id: str,
        feedback_count: int,
        report: dict,
        fixes: list,
    ) -> None:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO feedback_analysis
                   (batch_id, analysis_date, feedback_count, report_json, fixes_json,
                    fix_status, created_at, completed_at)
                   VALUES (?,?,?,?,?,'pending',?,?)""",
                (batch_id, batch_id, feedback_count,
                 json.dumps(report), json.dumps(fixes), now, now),
            )

    def get_recent_analyses(self, limit: int = 7) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback_analysis ORDER BY analysis_date DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["report_json"] = json.loads(d["report_json"] or "{}")
            d["fixes_json"] = json.loads(d["fixes_json"] or "[]")
            result.append(d)
        return result

    def add_fix_log(
        self,
        batch_id: str,
        fix_type: str,
        target_key: str,
        old_value: Optional[str],
        new_value: Optional[str],
        notes: str = "",
        applied: bool = False,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO feedback_fix_log
                   (batch_id, fix_type, target_key, old_value, new_value,
                    applied, applied_at, notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (batch_id, fix_type, target_key, old_value, new_value,
                 1 if applied else 0,
                 _now() if applied else None,
                 notes, _now()),
            )

    def get_adopted_fixes(self, limit: int = 10) -> list[dict]:
        """Return applied fixes with linked feedback for changelog attribution."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT fl.*, fa.analysis_date,
                   GROUP_CONCAT(DISTINCT f.user_name) as contributor_names
                   FROM feedback_fix_log fl
                   LEFT JOIN feedback_analysis fa ON fa.batch_id = fl.batch_id
                   LEFT JOIN feedback f ON f.analysis_batch_id = fl.batch_id
                       AND f.review_score >= 60 AND f.user_name != ''
                   WHERE fl.applied = 1
                   GROUP BY fl.id
                   ORDER BY fl.applied_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── System config ──────────────────────────────────────────────────────────

    def upsert_system_config(self, key: str, value: str, updated_by: str = "system") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO system_config (key, value, updated_at, updated_by)
                   VALUES (?,?,?,?)""",
                (key, value, _now(), updated_by),
            )

    def get_system_config(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    def list_system_configs(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM system_config").fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}
