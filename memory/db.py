"""SQLite long-term memory store."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from memory.schemas import BrandKit, UserPrefs, ProjectRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: str | Path = "./data/vah.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
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
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            text       TEXT NOT NULL,
            rating     INTEGER,
            created_at TEXT NOT NULL
        );
        """
        with self._conn() as conn:
            conn.executescript(ddl)

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
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO feedback (project_id,text,rating,created_at) VALUES (?,?,?,?)",
                (project_id, text, rating, _now()),
            )

    def get_feedback(self, project_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        return [dict(r) for r in rows]
