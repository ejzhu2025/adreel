"""web/server.py — FastAPI web interface for video-agent-hero."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Add project root to path so we can import agent.*
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import agent.deps as deps
from web.brand_kit_api import router as brand_kit_router

app = FastAPI(title="Video Agent Hero")
app.include_router(brand_kit_router)

# ── In-memory SSE state ───────────────────────────────────────────────────────

# Active queues for ongoing runs
_run_queues: dict[str, asyncio.Queue] = {}
# Stored events for completed/replayed runs
_run_events: dict[str, list[dict]] = {}


# ── Startup ───────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    deps.init()
    # Auto-seed brand kit + user prefs so the app works out of the box
    try:
        from memory.schemas import (
            BrandKit, UserPrefs, LogoConfig, ColorPalette, FontConfig,
            SubtitleStyle, IntroOutro,
        )
        from scripts.create_assets import create_placeholder_logo
        db = deps.db()
        logo_path = create_placeholder_logo()
        tong_sui = BrandKit(
            brand_id="tong_sui", name="Tong Sui",
            logo=LogoConfig(path=str(logo_path), safe_area="top_right"),
            colors=ColorPalette(primary="#00B894", secondary="#FFFFFF",
                                accent="#FF7675", background="#1A1A2E"),
            fonts=FontConfig(title="Poppins-SemiBold", body="Inter-Regular"),
            subtitle_style=SubtitleStyle(
                position="bottom_center", box_opacity=0.55, box_radius=12,
                padding_px=14, max_chars_per_line=18, highlight_keywords=True, font_size=44,
            ),
            intro_outro=IntroOutro(
                intro_template="mint_splash", outro_cta="Order now",
                intro_duration_sec=1.5, outro_duration_sec=2.0,
            ),
        )
        if not db.get_brand_kit("tong_sui"):
            db.upsert_brand_kit(tong_sui)
        ej = UserPrefs(
            user_id="ej", default_platform="tiktok", preferred_duration_sec=20,
            tone=["fresh", "playful", "premium"], pacing="fast", shot_density=7, cta_style="soft",
        )
        if not db.get_user_prefs("ej"):
            db.upsert_user_prefs(ej)
    except Exception as e:
        print(f"[startup] brand kit seed skipped: {e}", flush=True)


# ── Request models ────────────────────────────────────────────────────────────


class ApiKeyRequest(BaseModel):
    anthropic_api_key: str
    fal_key: str = ""


class CreateProjectRequest(BaseModel):
    brief: str
    brand_id: str = "tong_sui"
    user_id: str = "ej"


class RunRequest(BaseModel):
    skip_clarification: bool = True


class FeedbackRequest(BaseModel):
    text: str
    rating: int | None = None
    replan: bool = True


class PlanRequest(BaseModel):
    brief: str = ""
    brand_id: str = "tong_sui"
    user_id: str = "ej"
    clarification_answers: dict = {}
    plan_feedback: str = ""  # non-empty → replan from existing plan


class ExecuteRequest(BaseModel):
    plan: dict | None = None  # None → use DB-stored plan
    clarification_answers: dict = {}
    quality: str = "turbo"  # "turbo" | "hd"


class ModifyRequest(BaseModel):
    text: str
    quality: str = ""  # empty → inherit from current plan's _quality


# ── API endpoints ─────────────────────────────────────────────────────────────


@app.post("/api/init")
async def init_db():
    """Initialize DB with sample Tong Sui brand kit."""
    from memory.schemas import (
        BrandKit, UserPrefs, LogoConfig, ColorPalette, FontConfig,
        SubtitleStyle, IntroOutro,
    )
    from scripts.create_assets import create_placeholder_logo

    db = deps.db()
    logo_path = create_placeholder_logo()
    tong_sui = BrandKit(
        brand_id="tong_sui",
        name="Tong Sui",
        logo=LogoConfig(path=str(logo_path), safe_area="top_right"),
        colors=ColorPalette(
            primary="#00B894", secondary="#FFFFFF",
            accent="#FF7675", background="#1A1A2E",
        ),
        fonts=FontConfig(title="Poppins-SemiBold", body="Inter-Regular"),
        subtitle_style=SubtitleStyle(
            position="bottom_center", box_opacity=0.55, box_radius=12,
            padding_px=14, max_chars_per_line=18, highlight_keywords=True, font_size=44,
        ),
        intro_outro=IntroOutro(
            intro_template="mint_splash", outro_cta="Order now",
            intro_duration_sec=1.5, outro_duration_sec=2.0,
        ),
    )
    db.upsert_brand_kit(tong_sui)
    ej = UserPrefs(
        user_id="ej", default_platform="tiktok", preferred_duration_sec=20,
        tone=["fresh", "playful", "premium"], pacing="fast", shot_density=7, cta_style="soft",
    )
    db.upsert_user_prefs(ej)
    return {"status": "ok", "message": "DB initialized with Tong Sui brand kit"}


def _mask_key(key: str) -> str:
    return f"{key[:8]}…{key[-4:]}" if len(key) > 12 else ("set" if key else "")


def _upsert_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Write/update key=value pairs in a .env file without touching other lines."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    for var in updates:
        lines = [l for l in lines if not l.startswith(f"{var}=")]
    for var, val in updates.items():
        lines.append(f"{var}={val}")
    env_path.write_text("\n".join(lines) + "\n")


@app.get("/api/settings")
async def get_settings():
    """Return current settings (keys masked)."""
    ant_key = os.environ.get("ANTHROPIC_API_KEY", "")
    fal_key = os.environ.get("FAL_KEY", "") or os.environ.get("FAL_API_KEY", "")
    return {
        "anthropic_api_key_set": bool(ant_key),
        "anthropic_api_key_preview": _mask_key(ant_key),
        "fal_key_set": bool(fal_key),
        "fal_key_preview": _mask_key(fal_key),
    }


@app.post("/api/settings")
async def save_settings(req: ApiKeyRequest):
    """Set API keys for this session and persist to .env."""
    ant_key = req.anthropic_api_key.strip()
    fal_key = req.fal_key.strip()
    if not ant_key and not fal_key:
        raise HTTPException(status_code=400, detail="At least one API key must be provided")
    env_path = Path(__file__).parent.parent / ".env"
    updates: dict[str, str] = {}
    if ant_key:
        os.environ["ANTHROPIC_API_KEY"] = ant_key
        updates["ANTHROPIC_API_KEY"] = ant_key
    if fal_key:
        os.environ["FAL_KEY"] = fal_key
        updates["FAL_KEY"] = fal_key
    _upsert_env_file(env_path, updates)
    return {
        "status": "ok",
        "anthropic_preview": _mask_key(ant_key) if ant_key else None,
        "fal_preview": _mask_key(fal_key) if fal_key else None,
    }


@app.get("/video/{filename}")
async def serve_video(filename: str):
    """Serve a video file from the exports directory."""
    # Only allow mp4 files to prevent path traversal
    if not filename.endswith(".mp4") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    data_dir = os.environ.get("VAH_DATA_DIR", str(Path(__file__).parent.parent / "data"))
    video_path = Path(data_dir) / "exports" / filename
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(video_path, media_type="video/mp4")


@app.get("/api/projects")
async def list_projects():
    return deps.db().list_projects(limit=30)


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest):
    pid = deps.db().create_project(
        brief=req.brief, brand_id=req.brand_id, user_id=req.user_id,
    )
    return {"project_id": pid}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    # Attach product image flag so frontend knows to show thumbnail
    data_dir = Path(os.environ.get("VAH_DATA_DIR", "./data"))
    product_path = data_dir / "projects" / project_id / "product.png"
    proj["has_product_image"] = product_path.exists()
    return proj


def _get_project_product_image_path(project_id: str) -> str:
    data_dir = Path(os.environ.get("VAH_DATA_DIR", "./data"))
    p = data_dir / "projects" / project_id / "product.png"
    return str(p) if p.exists() else ""


@app.post("/api/projects/{project_id}/product-image")
async def upload_project_product_image(project_id: str, file: UploadFile = File(...)):
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    content = await file.read()
    if len(content) < 10:
        raise HTTPException(status_code=400, detail="File too small")
    data_dir = Path(os.environ.get("VAH_DATA_DIR", "./data"))
    proj_dir = data_dir / "projects" / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    product_path = proj_dir / "product.png"
    product_path.write_bytes(content)
    return {"status": "ok", "path": str(product_path)}


@app.get("/api/projects/{project_id}/product-image")
async def get_project_product_image(project_id: str):
    path = _get_project_product_image_path(project_id)
    if not path:
        raise HTTPException(status_code=404, detail="No product image for this project")
    return FileResponse(path)


@app.post("/api/projects/{project_id}/run")
async def run_project(
    project_id: str, req: RunRequest, background_tasks: BackgroundTasks
):
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    # Create queue before background task to avoid race condition
    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[project_id] = queue
    _run_events[project_id] = []

    deps.db().update_project_status(project_id, "running")
    background_tasks.add_task(
        _run_agent,
        project_id=project_id,
        proj=proj,
        skip_clarification=req.skip_clarification,
        queue=queue,
    )
    return {"status": "started"}


@app.post("/api/projects/{project_id}/plan")
async def plan_project(
    project_id: str, req: PlanRequest, background_tasks: BackgroundTasks
):
    """Run planning-only phase; stores plan in DB and sets status='planned'."""
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    from agent.graph import build_plan_only_graph

    prefs = deps.db().get_user_prefs(proj["user_id"])
    answers = req.clarification_answers or {
        "platform": prefs.default_platform if prefs else "tiktok",
        "duration_sec": prefs.preferred_duration_sec if prefs else 20,
        "style_tone": prefs.tone if prefs else ["fresh"],
        "language": "en",
        "assets_available": "none",
    }
    brand_kit_obj = deps.db().get_brand_kit(proj["brand_id"])
    # Load existing plan for replan case (feedback → modify existing plan)
    existing_plan = proj.get("latest_plan_json") or {} if req.plan_feedback else {}
    initial_state: dict = {
        "project_id": project_id,
        "brief": proj["brief"],
        "brand_id": proj["brand_id"],
        "user_id": proj["user_id"],
        "messages": [],
        "clarification_answers": answers,
        "plan_version": existing_plan.get("version", 0) if existing_plan else 0,
        "plan_feedback": req.plan_feedback,
        "qc_attempt": 1,
        "needs_replan": bool(req.plan_feedback),
        "brand_kit": brand_kit_obj.model_dump() if brand_kit_obj else {},
        "user_prefs": {},
        "similar_projects": [],
    }
    if existing_plan:
        initial_state["plan"] = existing_plan

    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[project_id] = queue
    _run_events[project_id] = []

    def _on_plan_node(node_name: str, node_output: dict):
        if node_name in ("planner_llm", "plan_checker"):
            plan = node_output.get("plan")
            if plan:
                deps.db().update_project_plan(project_id, plan)
                deps.db().update_project_status(project_id, "planned")

    deps.db().update_project_status(project_id, "running")
    background_tasks.add_task(
        _run_agent_with_state,
        project_id=project_id,
        initial_state=initial_state,
        queue=queue,
        graph_fn=build_plan_only_graph,
        replan=False,
        on_node=_on_plan_node,
    )
    return {"status": "plan_started"}


@app.post("/api/projects/{project_id}/execute")
async def execute_project(
    project_id: str, req: ExecuteRequest, background_tasks: BackgroundTasks
):
    """Run execution-only phase using stored or provided plan."""
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    from agent.graph import build_execute_only_graph

    plan = req.plan or proj.get("latest_plan_json") or {}
    if not plan:
        raise HTTPException(status_code=400, detail="No plan found; run /plan first")

    answers = req.clarification_answers or {
        "platform": plan.get("platform", "tiktok"),
        "duration_sec": plan.get("duration_sec", 20),
        "style_tone": plan.get("style_tone", ["fresh"]),
        "language": plan.get("language", "en"),
        "assets_available": "none",
    }
    brand_kit_obj = deps.db().get_brand_kit(proj["brand_id"])
    quality = req.quality if req.quality in ("turbo", "hd") else "turbo"
    initial_state: dict = {
        "project_id": project_id,
        "brief": proj["brief"],
        "brand_id": proj["brand_id"],
        "user_id": proj["user_id"],
        "messages": [],
        "clarification_answers": answers,
        "plan": plan,
        "plan_version": plan.get("version", 1),
        "qc_attempt": 1,
        "needs_replan": False,
        "brand_kit": brand_kit_obj.model_dump() if brand_kit_obj else {},
        "user_prefs": {},
        "similar_projects": [],
        "quality": quality,
    }

    # Track quality in the stored plan so the frontend can show the upgrade button
    def _on_execute_node(node_name: str, node_output: dict):
        if node_name == "result_summarizer":
            updated_plan = {**plan, "_quality": quality}
            deps.db().update_project_plan(project_id, updated_plan)

    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[project_id] = queue
    _run_events[project_id] = []

    deps.db().update_project_status(project_id, "running")
    background_tasks.add_task(
        _run_agent_with_state,
        project_id=project_id,
        initial_state=initial_state,
        queue=queue,
        graph_fn=build_execute_only_graph,
        replan=False,
        on_node=_on_execute_node,
    )
    return {"status": "execute_started", "quality": quality}


@app.post("/api/projects/{project_id}/modify")
async def modify_project(
    project_id: str, req: ModifyRequest, background_tasks: BackgroundTasks
):
    """Smart modify: classify feedback → local (partial re-render) or global (full replan)."""
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    plan = proj.get("latest_plan_json") or {}
    if not plan:
        raise HTTPException(status_code=400, detail="No plan found; generate a video first")

    from agent.graph import build_partial_rerender_graph

    quality = req.quality if req.quality in ("turbo", "hd") else plan.get("_quality", "turbo")
    answers = {
        "platform": plan.get("platform", "tiktok"),
        "duration_sec": plan.get("duration_sec", 20),
        "style_tone": plan.get("style_tone", ["fresh"]),
        "language": plan.get("language", "en"),
        "assets_available": "none",
    }
    brand_kit_obj = deps.db().get_brand_kit(proj["brand_id"])
    initial_state: dict = {
        "project_id": project_id,
        "brief": proj["brief"],
        "brand_id": proj["brand_id"],
        "user_id": proj["user_id"],
        "messages": [],
        "clarification_answers": answers,
        "plan": plan,
        "plan_version": plan.get("version", 1),
        "plan_feedback": req.text,
        "qc_attempt": 1,
        "needs_replan": False,
        "brand_kit": brand_kit_obj.model_dump() if brand_kit_obj else {},
        "user_prefs": {},
        "similar_projects": [],
        "quality": quality,
    }

    def _on_modify_node(node_name: str, node_output: dict):
        # Persist updated plan whenever partial_executor or result_summarizer updates it
        if node_name in ("partial_executor", "result_summarizer"):
            new_plan = node_output.get("plan")
            if new_plan:
                deps.db().update_project_plan(project_id, {**new_plan, "_quality": quality})

    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[project_id] = queue
    _run_events[project_id] = []

    deps.db().update_project_status(project_id, "running")
    background_tasks.add_task(
        _run_agent_with_state,
        project_id=project_id,
        initial_state=initial_state,
        queue=queue,
        graph_fn=build_partial_rerender_graph,
        replan=False,
        on_node=_on_modify_node,
    )
    return {"status": "modify_started"}


@app.post("/api/projects/{project_id}/feedback")
async def submit_feedback(project_id: str, req: FeedbackRequest, background_tasks: BackgroundTasks):
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    deps.db().add_feedback(project_id, req.text, req.rating)

    if not req.replan:
        return {"status": "saved"}

    plan = proj.get("latest_plan_json") or {}
    answers = {
        "platform": plan.get("platform", "tiktok"),
        "duration_sec": plan.get("duration_sec", 20),
        "style_tone": plan.get("style_tone", ["fresh"]),
        "language": plan.get("language", "en"),
        "assets_available": "none",
    }
    brand_kit_obj = deps.db().get_brand_kit(proj["brand_id"])
    replan_state: dict = {
        "project_id": project_id, "brief": proj["brief"],
        "brand_id": proj["brand_id"], "user_id": proj["user_id"],
        "brand_kit": brand_kit_obj.model_dump() if brand_kit_obj else {},
        "user_prefs": {}, "similar_projects": [],
        "plan": plan, "plan_version": plan.get("version", 1),
        "plan_feedback": req.text, "clarification_answers": answers,
        "messages": [], "qc_attempt": 1, "needs_replan": True,
    }

    queue: asyncio.Queue = asyncio.Queue()
    _run_queues[project_id] = queue
    _run_events[project_id] = []
    from agent.graph import build_replan_graph
    deps.db().update_project_status(project_id, "running")
    background_tasks.add_task(
        _run_agent_with_state, project_id=project_id,
        initial_state=replan_state, queue=queue,
        graph_fn=build_replan_graph, replan=False,
    )
    return {"status": "replan_started"}


@app.get("/api/projects/{project_id}/events")
async def stream_events(project_id: str):
    """SSE stream of agent execution events for a project."""

    async def generate():
        # Send retry hint
        yield "retry: 3000\n\n"

        # Replay stored events if run already finished
        if project_id not in _run_queues and project_id in _run_events:
            for event in _run_events[project_id]:
                yield f"data: {json.dumps(event)}\n\n"
            return

        queue = _run_queues.get(project_id)
        if not queue:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No run in progress'})}\n\n"
            return

        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=300.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Stream timeout'})}\n\n"
        finally:
            _run_queues.pop(project_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Background agent runner ───────────────────────────────────────────────────


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[mGKHJA-Za-z]", "", text)


def _serialize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_serialize(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    elif hasattr(obj, "model_dump"):
        return _serialize(obj.model_dump())
    else:
        try:
            return str(obj)
        except Exception:
            return None


async def _run_agent(
    project_id: str, proj: dict, skip_clarification: bool, queue: asyncio.Queue
):
    db = deps.db()
    initial_state: dict = {
        "project_id": project_id,
        "brief": proj["brief"],
        "brand_id": proj["brand_id"],
        "user_id": proj["user_id"],
        "messages": [],
        "clarification_answers": {},
        "plan_version": 0,
        "qc_attempt": 1,
        "needs_replan": False,
    }
    if skip_clarification:
        prefs = db.get_user_prefs(proj["user_id"])
        initial_state["clarification_answers"] = {
            "platform": prefs.default_platform if prefs else "tiktok",
            "duration_sec": prefs.preferred_duration_sec if prefs else 20,
            "style_tone": prefs.tone if prefs else ["fresh"],
            "language": "en",
            "assets_available": "none",
        }

    # Attach project-level product image if uploaded
    product_img = _get_project_product_image_path(project_id)
    if product_img:
        initial_state["product_image_path"] = product_img

    from agent.graph import build_graph
    await _run_agent_with_state(project_id, initial_state, queue, graph_fn=build_graph)


async def _run_agent_with_state(
    project_id: str,
    initial_state: dict,
    queue: asyncio.Queue,
    graph_fn: Any,
    replan: bool = False,
    on_node: Any = None,
):
    # Inject project-level product image for all execution paths
    if "product_image_path" not in initial_state:
        img_path = _get_project_product_image_path(project_id)
        if img_path:
            initial_state["product_image_path"] = img_path
    loop = asyncio.get_running_loop()

    def _emit(event: dict):
        _run_events.setdefault(project_id, []).append(event)
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_in_thread():
        graph = graph_fn()
        stdout_buf = io.StringIO()

        try:
            node_started_at: dict[str, str] = {}

            with contextlib.redirect_stdout(stdout_buf):
                for mode, chunk in graph.stream(
                    initial_state, stream_mode=["updates", "tasks"]
                ):
                    captured = _strip_ansi(stdout_buf.getvalue()).strip()
                    stdout_buf.truncate(0)
                    stdout_buf.seek(0)

                    if mode == "tasks":
                        # chunk has 'triggers' when node is STARTING,
                        # has 'result'/'error' when node is DONE (we use updates for done)
                        if "triggers" in chunk:
                            node_name = chunk.get("name", "")
                            ts = datetime.now().isoformat()
                            node_started_at[node_name] = ts
                            _emit({
                                "type": "node_start",
                                "node": node_name,
                                "timestamp": ts,
                            })
                    elif mode == "updates":
                        for node_name, node_output in chunk.items():
                            ts = datetime.now().isoformat()
                            started = node_started_at.get(node_name, ts)
                            _emit({
                                "type": "node_done",
                                "node": node_name,
                                "data": _serialize(node_output),
                                "stdout": captured,
                                "timestamp": ts,
                                "started_at": started,
                            })
                            if on_node:
                                on_node(node_name, node_output)

            _emit({"type": "done", "timestamp": datetime.now().isoformat()})
            deps.db().update_project_status(project_id, "done")

        except Exception as exc:
            import traceback
            _emit({
                "type": "error",
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": datetime.now().isoformat(),
            })
            deps.db().update_project_status(project_id, "failed")

    await asyncio.to_thread(run_in_thread)


# ── HTML frontend ─────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Video Agent Hero</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif; }
  .sidebar { background: #161b22; border-right: 1px solid #30363d; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
  .card-hover:hover { border-color: #58a6ff; cursor: pointer; }
  .btn-primary { background: #238636; color: #fff; border: 1px solid #2ea043; }
  .btn-primary:hover { background: #2ea043; }
  .btn-secondary { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
  .btn-secondary:hover { background: #30363d; }
  .btn-danger { background: #da3633; color: #fff; border: 1px solid #f85149; }
  .btn-danger:hover { background: #f85149; }
  .btn-approve { background: #1f6feb; color: #fff; border: 1px solid #388bfd; }
  .btn-approve:hover { background: #388bfd; }
  .status-done { color: #3fb950; }
  .status-running { color: #d29922; }
  .status-failed { color: #f85149; }
  .status-pending { color: #8b949e; }
  .node-card { border-left: 3px solid #30363d; }
  .node-card.running { border-left-color: #d29922; }
  .node-card.done { border-left-color: #3fb950; }
  .node-card.error { border-left-color: #f85149; }
  .log-code { background: #0d1117; border: 1px solid #30363d; font-family: monospace; font-size: 12px; }
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: #161b22; }
  ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
  .spinner { animation: spin 1s linear infinite; display: inline-block; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  .fade-in { animation: fadeIn 0.3s ease-in; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  input, textarea, select { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px; }
  input:focus, textarea:focus, select:focus { outline: none; border-color: #58a6ff; }
  .chip { display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 9999px;
    font-size: 12px; cursor: pointer; border: 1px solid #30363d; background: #21262d;
    color: #8b949e; transition: all 0.15s; user-select: none; }
  .chip:hover { border-color: #58a6ff; color: #c9d1d9; }
  .chip.selected { background: #0d419d; border-color: #388bfd; color: #79c0ff; }
  .chat-input { background: #161b22; border: 1px solid #30363d; border-radius: 12px; }
  .chat-input:focus-within { border-color: #58a6ff; }
  .approve-bar { background: #0d2136; border: 1px solid #1f6feb; border-radius: 10px; }
</style>
</head>
<body class="h-screen flex flex-col overflow-hidden">

<!-- Header -->
<header class="flex items-center justify-between px-5 py-3 border-b border-gray-800 flex-shrink-0">
  <div class="flex items-center gap-3">
    <span class="text-xl">🎬</span>
    <h1 class="text-base font-semibold text-white">Video Agent Hero</h1>
    <span class="text-xs px-2 py-0.5 rounded-full bg-gray-800 text-gray-400">LangGraph</span>
  </div>
  <div class="flex gap-2 items-center">
    <div id="api-status" class="text-xs text-gray-600 hidden">
      <span class="text-yellow-500">⚠</span> No API key
    </div>
    <button onclick="initDb()" class="btn-secondary text-xs px-3 py-1.5 rounded-md">Init DB</button>
    <button onclick="openSettings()" class="btn-secondary text-xs px-3 py-1.5 rounded-md flex items-center gap-1">
      <span>⚙</span> API Key
    </button>
    <button onclick="newVideo()" class="btn-secondary text-xs px-3 py-1.5 rounded-md flex items-center gap-1">
      <span>+</span> New
    </button>
  </div>
</header>

<!-- Settings modal -->
<div id="settings-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/60">
  <div class="card w-full max-w-md p-6 mx-4">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-base font-semibold text-white">API Settings</h2>
      <button onclick="closeSettings()" class="text-gray-500 hover:text-gray-300 text-lg leading-none">✕</button>
    </div>
    <div class="mb-4">
      <label class="text-sm text-gray-400 mb-2 block">Anthropic API Key</label>
      <div class="relative">
        <input id="api-key-input" type="password" class="w-full text-sm p-2.5 pr-10 font-mono"
          placeholder="sk-ant-api03-…" autocomplete="off"/>
        <button onclick="toggleKeyVisibility()" class="absolute right-2.5 top-2.5 text-gray-500 hover:text-gray-300 text-sm">👁</button>
      </div>
      <p id="api-key-current" class="text-xs text-gray-600 mt-1.5"></p>
      <p class="text-xs text-gray-600 mt-1">
        Used by the LLM Planner node. Without a key, the mock planner is used.
      </p>
    </div>
    <div class="mb-4">
      <label class="text-sm text-gray-400 mb-2 block">fal.ai API Key</label>
      <div class="relative">
        <input id="fal-key-input" type="password" class="w-full text-sm p-2.5 pr-10 font-mono"
          placeholder="…" autocomplete="off"/>
        <button onclick="toggleFalKeyVisibility()" class="absolute right-2.5 top-2.5 text-gray-500 hover:text-gray-300 text-sm">👁</button>
      </div>
      <p id="fal-key-current" class="text-xs text-gray-600 mt-1.5"></p>
      <p class="text-xs text-gray-600 mt-1">
        Used for T2V video generation. Without a key, PIL placeholder clips are used.
      </p>
    </div>
    <div class="flex gap-2">
      <button onclick="saveApiKey()" class="btn-primary text-sm px-4 py-2 rounded-md flex-1">Save</button>
      <button onclick="closeSettings()" class="btn-secondary text-sm px-4 py-2 rounded-md">Cancel</button>
    </div>
  </div>
</div>

<!-- Brand Kit Modal -->
<div id="brand-kit-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/60">
  <div class="card w-full max-w-md p-6 mx-4" style="max-height:90vh;overflow-y:auto">
    <div class="flex items-center justify-between mb-4">
      <h2 id="bk-modal-title" class="text-base font-semibold text-white">New Brand Kit</h2>
      <button onclick="closeBrandKitModal()" class="text-gray-500 hover:text-gray-300 text-lg leading-none">✕</button>
    </div>
    <input type="hidden" id="bk-id"/>
    <!-- Name -->
    <div class="mb-4">
      <label class="text-sm text-gray-400 mb-1 block">Brand Name</label>
      <input id="bk-name" type="text" class="w-full text-sm p-2" placeholder="Nike"/>
    </div>
    <!-- Colors -->
    <div class="mb-4">
      <label class="text-sm text-gray-400 mb-2 block">Colors</label>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="text-xs text-gray-500 block mb-1">Primary</label>
          <div class="flex gap-1 items-center">
            <input id="bk-color-primary" type="color" value="#00B894" class="w-8 h-8 rounded cursor-pointer border-0 p-0" style="background:none"/>
            <input id="bk-color-primary-hex" type="text" value="#00B894" class="flex-1 text-xs p-1.5 font-mono" maxlength="7"/>
          </div>
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-1">Secondary</label>
          <div class="flex gap-1 items-center">
            <input id="bk-color-secondary" type="color" value="#FFFFFF" class="w-8 h-8 rounded cursor-pointer border-0 p-0" style="background:none"/>
            <input id="bk-color-secondary-hex" type="text" value="#FFFFFF" class="flex-1 text-xs p-1.5 font-mono" maxlength="7"/>
          </div>
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-1">Accent</label>
          <div class="flex gap-1 items-center">
            <input id="bk-color-accent" type="color" value="#FF7675" class="w-8 h-8 rounded cursor-pointer border-0 p-0" style="background:none"/>
            <input id="bk-color-accent-hex" type="text" value="#FF7675" class="flex-1 text-xs p-1.5 font-mono" maxlength="7"/>
          </div>
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-1">Background</label>
          <div class="flex gap-1 items-center">
            <input id="bk-color-background" type="color" value="#1A1A2E" class="w-8 h-8 rounded cursor-pointer border-0 p-0" style="background:none"/>
            <input id="bk-color-background-hex" type="text" value="#1A1A2E" class="flex-1 text-xs p-1.5 font-mono" maxlength="7"/>
          </div>
        </div>
      </div>
    </div>
    <!-- Logo position -->
    <div class="mb-4">
      <label class="text-sm text-gray-400 mb-1 block">Logo Position</label>
      <select id="bk-safe-area" class="w-full text-sm p-2">
        <option value="top_right">Top Right</option>
        <option value="top_left">Top Left</option>
        <option value="bottom_right">Bottom Right</option>
        <option value="bottom_left">Bottom Left</option>
      </select>
    </div>
    <!-- Logo -->
    <div id="bk-logo-section" class="mb-4 hidden">
      <label class="text-sm text-gray-400 mb-2 block">Logo</label>
      <div id="bk-logo-preview" class="mb-2 hidden">
        <img id="bk-logo-img" src="" alt="logo" class="h-12 rounded border border-gray-700 bg-gray-900 p-1"/>
      </div>
      <div class="flex gap-2 mb-2">
        <input id="bk-logo-url" type="text" class="flex-1 text-xs p-2" placeholder="https://nike.com"/>
        <button onclick="fetchLogoFromUrl()" class="btn-secondary text-xs px-3 py-1.5 rounded-md whitespace-nowrap">Fetch</button>
      </div>
      <div>
        <input id="bk-logo-file" type="file" accept="image/*" class="text-xs text-gray-400" onchange="uploadLogoFile(this)"/>
      </div>
    </div>
    <!-- Actions -->
    <div class="flex gap-2 mt-2">
      <button onclick="saveBrandKit()" class="btn-primary text-sm px-4 py-2 rounded-md flex-1">Save</button>
      <button id="bk-delete-btn" onclick="deleteBrandKit()" class="btn-danger text-sm px-4 py-2 rounded-md hidden">Delete</button>
      <button onclick="closeBrandKitModal()" class="btn-secondary text-sm px-4 py-2 rounded-md">Cancel</button>
    </div>
  </div>
</div>

<div class="flex flex-1 overflow-hidden">

<!-- Sidebar -->
<aside class="sidebar w-56 flex-shrink-0 flex flex-col overflow-hidden">
  <div class="p-2 border-b border-gray-800 text-xs text-gray-500 px-3 py-2 font-medium">Projects</div>
  <!-- Project list -->
  <div id="project-list" class="flex-1 overflow-y-auto p-2 space-y-1" style="min-height:0">
    <p class="text-xs text-gray-500 text-center pt-4">Loading...</p>
  </div>
  <!-- Brand Kits section -->
  <div class="border-t border-gray-800 flex-shrink-0">
    <div class="flex items-center justify-between px-3 py-2">
      <span class="text-xs text-gray-500 font-medium">Brand Kits</span>
      <button onclick="openNewBrandKitModal()" class="text-xs text-blue-400 hover:text-blue-300 leading-none">+ New</button>
    </div>
    <div id="brand-kit-list" class="px-2 pb-2 space-y-1 max-h-36 overflow-y-auto">
      <p class="text-xs text-gray-600 text-center py-1">Loading...</p>
    </div>
  </div>
</aside>

<!-- Main content -->
<main class="flex-1 overflow-hidden flex flex-col">

  <!-- Body: log/plan pane + video panel -->
  <div class="flex flex-1 overflow-hidden">

    <!-- Left pane: tabs + chat bar -->
    <div class="flex-1 flex flex-col overflow-hidden border-r border-gray-800">

      <!-- Project info strip (shown when project selected) -->
      <div id="proj-info-strip" class="hidden px-4 py-2 border-b border-gray-800 flex items-center gap-3 flex-shrink-0">
        <span id="proj-status-dot" class="text-xs font-mono px-2 py-0.5 rounded-full bg-gray-800"></span>
        <span id="proj-id" class="text-xs text-gray-500 font-mono"></span>
        <p id="proj-brief" class="text-xs text-gray-400 truncate flex-1"></p>
        <!-- Product image indicator -->
        <div id="proj-product-thumb" class="hidden flex items-center gap-1.5 flex-shrink-0">
          <img id="proj-product-img" src="" alt="product" class="h-7 w-7 object-cover rounded border border-gray-600" title="Product image attached"/>
          <button onclick="document.getElementById('product-file-input').click()"
            class="text-xs text-gray-600 hover:text-gray-400" title="Replace product image">↺</button>
        </div>
        <button id="proj-attach-btn" onclick="document.getElementById('product-file-input').click()"
          class="hidden text-xs text-gray-600 hover:text-gray-400 flex-shrink-0 flex items-center gap-1" title="Add product image">
          📎 Add product image
        </button>
      </div>

      <!-- Tab bar -->
      <div class="flex border-b border-gray-800 px-4 flex-shrink-0">
        <button onclick="switchTab('log')" id="tab-log"
          class="tab-btn text-xs py-2.5 px-3 border-b-2 border-blue-500 text-blue-400 font-medium">
          Agent Steps
        </button>
        <button onclick="switchTab('plan')" id="tab-plan"
          class="tab-btn text-xs py-2.5 px-3 border-b-2 border-transparent text-gray-500 hover:text-gray-400">
          Storyboard &amp; Plan
        </button>
      </div>

      <!-- Log pane -->
      <div id="pane-log" class="flex-1 overflow-y-auto p-4">
        <div id="agent-log-empty" class="text-center text-gray-600 py-12">
          <div class="text-4xl mb-3">🎬</div>
          <p class="text-sm text-gray-500">Describe your video below to get started</p>
        </div>
        <div id="agent-log" class="space-y-3 hidden"></div>
      </div>

      <!-- Plan pane -->
      <div id="pane-plan" class="hidden flex-1 overflow-y-auto p-4">
        <div id="plan-empty" class="text-center text-gray-600 py-12">
          <p class="text-sm">No plan yet — send a brief to start</p>
        </div>
        <div id="plan-content" class="hidden space-y-5"></div>
      </div>

      <!-- Approve bar (visible in plan_ready state) -->
      <div id="approve-bar" class="hidden px-4 py-3 border-t border-gray-800 flex-shrink-0">
        <div class="approve-bar p-3 flex items-center gap-3">
          <div class="flex-1">
            <p class="text-sm font-medium text-blue-300">Plan ready — review the storyboard</p>
            <p class="text-xs text-gray-500 mt-0.5">Edit scenes above, then approve to generate</p>
          </div>
          <button onclick="approveAndGenerate()"
            class="btn-approve text-sm px-5 py-2 rounded-md font-medium flex items-center gap-2 flex-shrink-0">
            <span>▶</span> Approve &amp; Generate
          </button>
        </div>
      </div>

      <!-- Chat bar -->
      <div id="chat-bar" class="px-4 py-3 border-t border-gray-800 flex-shrink-0">
        <!-- Chips row -->
        <div id="chips-row" class="flex items-center gap-1.5 mb-2 flex-wrap">
          <span class="text-xs text-gray-600 mr-1">Aspect:</span>
          <span class="chip selected" data-aspect="9:16" onclick="selectAspect(this)">9:16 ↕</span>
          <span class="chip" data-aspect="16:9" onclick="selectAspect(this)">16:9 ↔</span>
          <span class="chip" data-aspect="1:1" onclick="selectAspect(this)">1:1 ▣</span>
          <span class="text-gray-700 mx-1">|</span>
          <span class="text-xs text-gray-600 mr-1">Duration:</span>
          <span class="chip selected" data-dur="5" onclick="selectDuration(this)">5s</span>
          <span class="chip" data-dur="10" onclick="selectDuration(this)">10s</span>
          <span class="text-gray-700 mx-1">|</span>
          <span class="text-xs text-gray-600 mr-1">Style:</span>
          <span class="chip selected" data-style="fresh" onclick="toggleStyle(this)">fresh</span>
          <span class="chip" data-style="playful" onclick="toggleStyle(this)">playful</span>
          <span class="chip" data-style="premium" onclick="toggleStyle(this)">premium</span>
          <span class="chip" data-style="promo" onclick="toggleStyle(this)">promo</span>
          <span class="text-gray-700 mx-1">|</span>
          <span class="text-xs text-gray-600 mr-1">Brand:</span>
          <select id="brand-select" class="text-xs px-2 py-0.5 rounded"
            style="background:#0d1117;border:1px solid #30363d;color:#c9d1d9"
            onchange="selectedBrandId = this.value">
            <option value="tong_sui">Tong Sui</option>
          </select>
        </div>
        <!-- Product image preview (shown when attached) -->
        <div id="product-preview-bar" class="hidden flex items-center gap-2 mb-2 px-1">
          <img id="product-preview-img" src="" alt="product" class="h-10 w-10 object-cover rounded border border-gray-600"/>
          <div class="flex-1">
            <p class="text-xs text-gray-400" id="product-preview-name"></p>
            <p class="text-xs text-gray-600">Product image — will be used for ad outro</p>
          </div>
          <button onclick="clearProductImage()" class="text-gray-600 hover:text-gray-300 text-sm">✕</button>
        </div>
        <!-- Input row -->
        <div class="chat-input flex items-end gap-2 px-3 py-2">
          <!-- Hidden file input -->
          <input id="product-file-input" type="file" accept="image/*" class="hidden" onchange="handleProductImageSelect(this)"/>
          <button id="attach-btn" onclick="document.getElementById('product-file-input').click()"
            title="Attach product image"
            class="text-gray-500 hover:text-gray-300 text-lg flex-shrink-0 self-end pb-1 transition-colors" style="line-height:1">
            📎
          </button>
          <textarea id="chat-input" rows="2"
            class="flex-1 text-sm bg-transparent border-none outline-none resize-none text-gray-200"
            placeholder="Describe your video..."
            oninput="this.style.height='auto';this.style.height=Math.min(this.scrollHeight,120)+'px'"></textarea>
          <button id="chat-send-btn" onclick="handleChatSend()"
            class="btn-primary text-sm px-4 py-1.5 rounded-lg font-medium flex-shrink-0 self-end">
            Send
          </button>
        </div>
      </div>

    </div>

    <!-- Video panel -->
    <div id="video-panel" class="w-56 flex-shrink-0 flex flex-col p-4 gap-3 overflow-y-auto">
      <div id="video-empty" class="flex-1 flex flex-col items-center justify-center text-gray-700 gap-2">
        <span class="text-4xl">🎞</span>
        <p class="text-xs text-center">Output video will appear here after generation</p>
      </div>
      <div id="video-outputs" class="hidden space-y-3"></div>
    </div>

  </div>
</main>

</div>

<!-- Toast -->
<div id="toast" class="fixed bottom-4 right-4 hidden">
  <div class="bg-gray-800 border border-gray-700 text-sm px-4 py-2 rounded-lg shadow-lg"></div>
</div>

<script>
// ── App state ──────────────────────────────────────────────────────────────
let currentProjectId = null;
let eventSource = null;
let appState = 'idle'; // idle | planning | plan_ready | executing | done | error
let currentPlan = null;
let selectedDuration = 5;
let selectedStyles = ['fresh'];
let selectedAspect = '9:16';
let selectedBrandId = 'tong_sui';

// ── Node metadata ──────────────────────────────────────────────────────────
const NODE_META = {
  intent_parser:        { icon: '🔍', label: 'Intent Parser',       desc: 'Extracting hints from brief' },
  memory_loader:        { icon: '🧠', label: 'Memory Loader',       desc: 'Loading brand kit & preferences' },
  clarification_planner:{ icon: '❓', label: 'Clarification Planner', desc: 'Checking required fields' },
  ask_user:             { icon: '💬', label: 'Ask User',            desc: 'Collecting clarification answers' },
  planner_llm:          { icon: '✨', label: 'LLM Planner',         desc: 'Generating video plan with AI' },
  plan_checker:         { icon: '✅', label: 'Plan Checker',        desc: 'Validating & fixing plan' },
  change_classifier:    { icon: '🔀', label: 'Change Classifier',   desc: 'Analyzing modification scope' },
  partial_executor:     { icon: '🎯', label: 'Partial Re-render',   desc: 'Regenerating affected shots only' },
  executor_pipeline:    { icon: '🎬', label: 'Video Renderer',      desc: 'Rendering video clips' },
  caption_agent:        { icon: '📝', label: 'Caption Agent',       desc: 'Generating caption segments' },
  layout_branding:      { icon: '🎨', label: 'Layout & Branding',   desc: 'Applying subtitles & watermark' },
  quality_gate:         { icon: '🔎', label: 'Quality Gate',        desc: 'Checking output quality' },
  render_export:        { icon: '📤', label: 'Export',              desc: 'Final H.264 video export' },
  result_summarizer:    { icon: '📊', label: 'Result Summarizer',   desc: 'Generating run summary' },
  memory_writer:        { icon: '💾', label: 'Memory Writer',       desc: 'Saving to DB & vector store' },
};

function getNodeSummary(node, data) {
  try {
    switch (node) {
      case 'intent_parser': {
        const a = data.clarification_answers || {};
        const parts = [];
        if (a.platform) parts.push(`platform: ${a.platform}`);
        if (a.duration_sec) parts.push(`${a.duration_sec}s`);
        if (a.style_tone) parts.push(Array.isArray(a.style_tone) ? a.style_tone.join(', ') : a.style_tone);
        return parts.length ? parts.join(' · ') : 'Brief parsed';
      }
      case 'memory_loader': {
        const bk = data.brand_kit || {};
        const sim = (data.similar_projects || []).length;
        return `Brand: ${bk.name || bk.brand_id || '—'} · ${sim} similar project${sim !== 1 ? 's' : ''}`;
      }
      case 'clarification_planner': {
        if (data.clarification_needed) {
          const n = (data.clarification_questions || []).length;
          return `${n} question${n !== 1 ? 's' : ''} needed — routing to ask_user`;
        }
        return 'All fields answered — proceeding to planner';
      }
      case 'ask_user':
        return 'Clarification answers collected';
      case 'planner_llm': {
        const plan = data.plan || {};
        const shots = (plan.shot_list || []).length;
        const hook = plan.script?.hook || '';
        const v = data.plan_version || 1;
        return `v${v} · ${shots} shots · ${plan.platform || '?'} · ${plan.duration_sec || '?'}s` +
          (hook ? ` · "${hook.slice(0, 50)}${hook.length > 50 ? '…' : ''}"` : '');
      }
      case 'plan_checker': {
        if (data.needs_replan) return '⚠ Issues found — triggering replan';
        const shots = (data.plan?.shot_list || []).length;
        return `✓ Plan valid · ${shots} shots`;
      }
      case 'executor_pipeline': {
        const clips = (data.scene_clips || []).length;
        return `${clips} clip${clips !== 1 ? 's' : ''} rendered`;
      }
      case 'caption_agent': {
        const segs = (data.caption_segments || []).length;
        return `${segs} caption segment${segs !== 1 ? 's' : ''} generated`;
      }
      case 'layout_branding': {
        const p = data.branded_clip_path || '';
        return p ? `Branded: ${p.split('/').pop()}` : 'Branding applied';
      }
      case 'quality_gate': {
        const qr = data.quality_result || {};
        if (qr.passed) return '✓ All QC checks passed';
        const issues = (qr.issues || []).join(', ');
        return `⚠ ${issues || 'Issues found'}${qr.auto_fix_applied ? ' (auto-fixed)' : ''}`;
      }
      case 'render_export': {
        const p = data.output_path || '';
        return p ? `📁 ${p.split('/').pop()}` : 'Export complete';
      }
      case 'result_summarizer': {
        const s = data.summary || '';
        return s ? s.slice(0, 100) + (s.length > 100 ? '…' : '') : 'Summary generated';
      }
      case 'change_classifier': {
        const ct = data.change_type;
        const shots = (data.affected_shot_indices || []);
        if (ct === 'local') return `Local change — re-rendering shot${shots.length !== 1 ? 's' : ''} [${shots.join(', ')}]`;
        return 'Global change — replanning entire video';
      }
      case 'partial_executor': {
        const clips = (data.scene_clips || []).length;
        const affected = (data.messages || []).find(m => m.content?.includes('partial_executor'));
        return clips ? `${clips} clips ready (partial re-render)` : 'Partial re-render complete';
      }
      case 'memory_writer':
        return 'Saved to SQLite & ChromaDB vector store';
      default:
        return '';
    }
  } catch (e) { return ''; }
}

// ── API helpers ────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  const inner = el.querySelector('div');
  inner.textContent = msg;
  inner.className = `text-sm px-4 py-2 rounded-lg shadow-lg ${
    type === 'error' ? 'bg-red-900 border-red-700 text-red-100' :
    type === 'success' ? 'bg-green-900 border-green-700 text-green-100' :
    'bg-gray-800 border-gray-700 text-gray-100'
  }`;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 3000);
}

// ── Chip selection ─────────────────────────────────────────────────────────
function selectAspect(el) {
  document.querySelectorAll('[data-aspect]').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  selectedAspect = el.dataset.aspect;
}

function selectDuration(el) {
  document.querySelectorAll('[data-dur]').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  selectedDuration = parseInt(el.dataset.dur);
}

function toggleStyle(el) {
  const s = el.dataset.style;
  if (el.classList.contains('selected')) {
    if (selectedStyles.length > 1) {
      el.classList.remove('selected');
      selectedStyles = selectedStyles.filter(x => x !== s);
    }
  } else {
    el.classList.add('selected');
    selectedStyles.push(s);
  }
}

// ── App state machine ──────────────────────────────────────────────────────
function setAppState(state) {
  appState = state;
  const input = document.getElementById('chat-input');
  const sendBtn = document.getElementById('chat-send-btn');
  const chipsRow = document.getElementById('chips-row');
  const approveBar = document.getElementById('approve-bar');

  const cfg = {
    idle:       { placeholder: 'Describe your video...', sendLabel: 'Send',   chips: true,  approve: false, inputDisabled: false },
    planning:   { placeholder: 'Planning…',              sendLabel: '…',      chips: false, approve: false, inputDisabled: true  },
    plan_ready: { placeholder: 'Ask to change the plan...', sendLabel: 'Replan', chips: true, approve: true, inputDisabled: false },
    executing:  { placeholder: 'Generating…',            sendLabel: '…',      chips: false, approve: false, inputDisabled: true  },
    done:       { placeholder: 'Modify this video...',   sendLabel: 'Modify', chips: false, approve: false, inputDisabled: false },
    error:      { placeholder: 'Describe your video...', sendLabel: 'Send',   chips: true,  approve: false, inputDisabled: false },
  }[state] || { placeholder: '', sendLabel: 'Send', chips: true, approve: false, inputDisabled: false };

  input.placeholder = cfg.placeholder;
  input.disabled = cfg.inputDisabled;
  sendBtn.textContent = cfg.sendLabel;
  sendBtn.disabled = cfg.inputDisabled;
  chipsRow.classList.toggle('hidden', !cfg.chips);
  approveBar.classList.toggle('hidden', !cfg.approve);
}

// ── Project list ───────────────────────────────────────────────────────────
const _run_events_cache = {};

async function loadProjects() {
  try {
    const projects = await api('GET', '/api/projects');
    renderProjectList(projects);
  } catch (e) {
    document.getElementById('project-list').innerHTML =
      `<p class="text-xs text-red-400 text-center pt-4">${e.message}</p>`;
  }
}

function renderProjectList(projects) {
  const el = document.getElementById('project-list');
  if (!projects.length) {
    el.innerHTML = '<p class="text-xs text-gray-500 text-center pt-4">No projects yet</p>';
    return;
  }
  el.innerHTML = projects.map(p => {
    const statusClass = {
      done: 'status-done', running: 'status-running', failed: 'status-failed', planned: 'text-blue-400'
    }[p.status] || 'status-pending';
    const dot = { done: '●', running: '◉', failed: '✕', pending: '○', planned: '◑' }[p.status] || '○';
    const brief = p.brief.length > 45 ? p.brief.slice(0, 45) + '…' : p.brief;
    const active = p.project_id === currentProjectId ? 'border-blue-500 bg-blue-950/20' : '';
    return `
      <div onclick="selectProject('${p.project_id}')"
        class="card card-hover p-2 ${active} transition-colors">
        <div class="flex items-center gap-1.5 mb-1">
          <span class="${statusClass} text-xs">${dot}</span>
          <span class="text-xs text-gray-500 font-mono">${p.project_id.slice(0, 8)}</span>
          <span class="text-xs text-gray-600 ml-auto">${p.status}</span>
        </div>
        <p class="text-xs text-gray-400 leading-relaxed">${brief}</p>
      </div>`;
  }).join('');
}

async function selectProject(id) {
  currentProjectId = id;
  try {
    const proj = await api('GET', `/api/projects/${id}`);
    updateProjStrip(proj);
    loadProjects();

    // Reset video panel
    document.getElementById('video-empty').classList.remove('hidden');
    document.getElementById('video-outputs').classList.add('hidden');
    document.getElementById('video-outputs').innerHTML = '';

    clearAgentLog();

    if (proj.status === 'done') {
      setAppState('done');
      if (_run_events_cache[id]) replayEvents(_run_events_cache[id]);
      if ((proj.output_paths || []).length) loadProjectVideo(id);
      if (proj.latest_plan_json) {
        currentPlan = proj.latest_plan_json;
        renderPlan(currentPlan, false);
      }
    } else if (proj.status === 'planned') {
      setAppState('plan_ready');
      if (proj.latest_plan_json) {
        currentPlan = proj.latest_plan_json;
        renderPlan(currentPlan, true);
        switchTab('plan');
      }
    } else if (proj.status === 'running') {
      setAppState('executing');
      if (_run_events_cache[id]) replayEvents(_run_events_cache[id]);
      else connectEventStream(id);
    } else {
      setAppState('idle');
      if (_run_events_cache[id]) replayEvents(_run_events_cache[id]);
    }
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Product image (project-level) ──────────────────────────────────────────
let pendingProductFile = null; // File object waiting to be uploaded after project creation

function handleProductImageSelect(input) {
  const file = input.files[0];
  if (!file) return;
  pendingProductFile = file;

  // Show preview in chat bar
  const reader = new FileReader();
  reader.onload = (e) => {
    document.getElementById('product-preview-img').src = e.target.result;
    document.getElementById('product-preview-name').textContent = file.name;
    document.getElementById('product-preview-bar').classList.remove('hidden');
    document.getElementById('attach-btn').classList.add('text-blue-400');
  };
  reader.readAsDataURL(file);

  // If a project is already selected, upload immediately
  if (currentProjectId) uploadProductImageForProject(currentProjectId, file);
}

function clearProductImage() {
  pendingProductFile = null;
  document.getElementById('product-file-input').value = '';
  document.getElementById('product-preview-bar').classList.add('hidden');
  document.getElementById('attach-btn').classList.remove('text-blue-400');
}

async function uploadProductImageForProject(projectId, file) {
  try {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch(`/api/projects/${projectId}/product-image`, { method: 'POST', body: fd });
    if (!res.ok) throw new Error((await res.json()).detail || 'Upload failed');
    // Refresh strip to show thumbnail
    const proj = await api('GET', `/api/projects/${projectId}`);
    updateProjStrip(proj);
    toast('Product image uploaded', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

function updateProjStrip(proj) {
  const strip = document.getElementById('proj-info-strip');
  strip.classList.remove('hidden');
  const statusLabels = { done: '✓ done', running: '⟳ running', failed: '✕ failed', pending: '● pending', planned: '◑ planned' };
  const statusColors = { done: 'text-green-400', running: 'text-yellow-400', failed: 'text-red-400', pending: 'text-gray-400', planned: 'text-blue-400' };
  const dot = document.getElementById('proj-status-dot');
  dot.textContent = statusLabels[proj.status] || proj.status;
  dot.className = `text-xs font-mono px-2 py-0.5 rounded-full bg-gray-800 ${statusColors[proj.status] || ''}`;
  document.getElementById('proj-id').textContent = proj.project_id.slice(0, 8);
  document.getElementById('proj-brief').textContent = proj.brief;

  // Show/hide product image thumbnail in strip
  const thumb = document.getElementById('proj-product-thumb');
  const attachBtn = document.getElementById('proj-attach-btn');
  if (proj.has_product_image) {
    document.getElementById('proj-product-img').src = `/api/projects/${proj.project_id}/product-image?t=${Date.now()}`;
    thumb.classList.remove('hidden');
    attachBtn.classList.add('hidden');
  } else {
    thumb.classList.add('hidden');
    attachBtn.classList.remove('hidden');
  }
}

// ── Chat send handler ──────────────────────────────────────────────────────
async function handleChatSend() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;

  if (appState === 'idle' || appState === 'error') {
    input.value = '';
    input.style.height = 'auto';
    await createAndPlan(text);
  } else if (appState === 'plan_ready') {
    input.value = '';
    input.style.height = 'auto';
    await replanWithText(text);
  } else if (appState === 'done') {
    input.value = '';
    input.style.height = 'auto';
    await submitFeedback(text);
  }
}

async function createAndPlan(brief) {
  try {
    setAppState('planning');
    clearAgentLog();
    showAgentLog();

    const answers = {
      duration_sec: selectedDuration,
      style_tone: [...selectedStyles],
      platform: 'tiktok',
      language: 'en',
      assets_available: 'none',
    };

    const res = await api('POST', '/api/projects', { brief, brand_id: selectedBrandId, user_id: 'ej' });
    currentProjectId = res.project_id;
    _run_events_cache[currentProjectId] = [];
    await loadProjects();

    // Upload pending product image (attached before project was created)
    if (pendingProductFile) {
      await uploadProductImageForProject(currentProjectId, pendingProductFile);
      clearProductImage();
    }

    await api('POST', `/api/projects/${currentProjectId}/plan`, { clarification_answers: answers });
    connectEventStream(currentProjectId, 'plan');
  } catch (e) {
    toast(e.message, 'error');
    setAppState('error');
  }
}

async function replanWithText(text) {
  if (!currentProjectId) return;
  try {
    setAppState('planning');
    clearAgentLog();
    showAgentLog();
    switchTab('log');
    _run_events_cache[currentProjectId] = [];

    const answers = {
      duration_sec: selectedDuration,
      style_tone: [...selectedStyles],
      platform: 'tiktok',
      language: 'en',
      assets_available: 'none',
    };
    await api('POST', `/api/projects/${currentProjectId}/plan`, {
      clarification_answers: answers,
      plan_feedback: text,
    });
    connectEventStream(currentProjectId, 'plan');
  } catch (e) {
    toast(e.message, 'error');
    setAppState('error');
  }
}

async function approveAndGenerate(quality = 'turbo') {
  if (!currentProjectId) return;
  try {
    const plan = collectModifiedPlan();
    setAppState('executing');
    clearAgentLog();
    showAgentLog();
    switchTab('log');
    _run_events_cache[currentProjectId] = [];

    await api('POST', `/api/projects/${currentProjectId}/execute`, {
      plan: plan,
      quality: quality,
      clarification_answers: {
        platform: plan.platform || 'tiktok',
        duration_sec: plan.duration_sec || selectedDuration,
        style_tone: plan.style_tone || selectedStyles,
        language: plan.language || 'en',
        assets_available: 'none',
      },
    });
    connectEventStream(currentProjectId, 'execute');
  } catch (e) {
    toast(e.message, 'error');
    setAppState('error');
  }
}

async function upgradeToHD() {
  if (!currentProjectId || !currentPlan) return;
  try {
    setAppState('executing');
    clearAgentLog();
    showAgentLog();
    switchTab('log');
    _run_events_cache[currentProjectId] = [];

    await api('POST', `/api/projects/${currentProjectId}/execute`, {
      plan: currentPlan,
      quality: 'hd',
      clarification_answers: {
        platform: currentPlan.platform || 'tiktok',
        duration_sec: currentPlan.duration_sec || selectedDuration,
        style_tone: currentPlan.style_tone || selectedStyles,
        language: currentPlan.language || 'en',
        assets_available: 'none',
      },
    });
    connectEventStream(currentProjectId, 'execute');
  } catch (e) {
    toast(e.message, 'error');
    setAppState('error');
  }
}

async function submitFeedback(text) {
  if (!currentProjectId) return;
  try {
    setAppState('executing');
    clearAgentLog();
    showAgentLog();
    switchTab('log');
    _run_events_cache[currentProjectId] = [];
    const quality = currentPlan?._quality || 'turbo';
    const res = await api('POST', `/api/projects/${currentProjectId}/modify`, { text, quality });
    if (res.status === 'modify_started') {
      connectEventStream(currentProjectId, 'execute');
    }
    loadProjects();
  } catch (e) {
    toast(e.message, 'error');
    setAppState('error');
  }
}

function collectModifiedPlan() {
  if (!currentPlan) return {};
  const plan = JSON.parse(JSON.stringify(currentPlan));
  document.querySelectorAll('[data-scene-idx]').forEach(card => {
    const i = parseInt(card.dataset.sceneIdx);
    const descEl = card.querySelector('[data-field="desc"]');
    const durEl = card.querySelector('[data-field="duration"]');
    const overlayEl = card.querySelector('[data-field="overlay"]');
    if (descEl && plan.storyboard && plan.storyboard[i]) {
      plan.storyboard[i].desc = descEl.value;
    }
    if (durEl) {
      const dur = parseFloat(durEl.value);
      if (!isNaN(dur)) {
        if (plan.storyboard && plan.storyboard[i]) plan.storyboard[i].duration = dur;
        if (plan.shot_list && plan.shot_list[i]) plan.shot_list[i].duration = dur;
      }
    }
    if (overlayEl && plan.shot_list && plan.shot_list[i]) {
      plan.shot_list[i].text_overlay = overlayEl.value;
    }
  });
  return plan;
}

function newVideo() {
  currentProjectId = null;
  currentPlan = null;
  clearAgentLog();
  clearProductImage();
  setAppState('idle');
  document.getElementById('proj-info-strip').classList.add('hidden');
  document.getElementById('video-empty').classList.remove('hidden');
  document.getElementById('video-outputs').classList.add('hidden');
  document.getElementById('video-outputs').innerHTML = '';
  document.getElementById('plan-content').classList.add('hidden');
  document.getElementById('plan-empty').classList.remove('hidden');
  switchTab('log');
  document.getElementById('chat-input').focus();
  loadProjects();
}

function connectEventStream(projectId, phase) {
  if (eventSource) { eventSource.close(); }

  eventSource = new EventSource(`/api/projects/${projectId}/events`);

  eventSource.onmessage = (e) => {
    const event = JSON.parse(e.data);
    _run_events_cache[projectId] = _run_events_cache[projectId] || [];
    _run_events_cache[projectId].push(event);
    handleEvent(event, projectId, phase);
  };

  eventSource.onerror = () => {
    eventSource.close();
    eventSource = null;
  };
}

function handleEvent(event, projectId, phase) {
  if (event.type === 'node_start') {
    addNodeSpinner(event.node, event.timestamp);
  } else if (event.type === 'node_done') {
    addNodeCard(event.node, event.data, event.stdout, event.timestamp);
    if (event.node === 'plan_checker' && event.data && event.data.plan) {
      currentPlan = event.data.plan;
    }
    if (event.node === 'planner_llm' && event.data && event.data.plan) {
      currentPlan = event.data.plan;
    }
    if (event.node === 'qc_diagnose' && event.data && event.data.needs_user_action) {
      addQcDiagnoseAlert(event.data.qc_user_message, event.data.qc_diagnosis);
      setAppState('error');
      if (eventSource) { eventSource.close(); eventSource = null; }
      return;
    }
  } else if (event.type === 'done') {
    if (eventSource) { eventSource.close(); eventSource = null; }
    loadProjects();
    if (phase === 'plan') {
      // Planning done → load plan, switch to plan tab, show approve bar
      api('GET', `/api/projects/${projectId}`).then(proj => {
        if (proj.latest_plan_json) {
          currentPlan = proj.latest_plan_json;
          renderPlan(currentPlan, true);
          switchTab('plan');
        }
        updateProjStrip(proj);
      }).catch(() => {});
      setAppState('plan_ready');
      addPlanDoneCard();
    } else {
      // Execute done → load video
      addDoneCard(projectId);
      setAppState('done');
      api('GET', `/api/projects/${projectId}`).then(proj => {
        updateProjStrip(proj);
        if (proj.latest_plan_json) {
          currentPlan = proj.latest_plan_json;
          renderPlan(currentPlan, false);
        }
      }).catch(() => {});
    }
  } else if (event.type === 'error') {
    addErrorCard(event.message, event.traceback);
    setAppState('error');
    if (eventSource) { eventSource.close(); eventSource = null; }
    loadProjects();
  }
}

function replayEvents(events) {
  showAgentLog();
  for (const event of events) {
    handleEvent(event, currentProjectId, null);
  }
}

function showAgentLog() {
  document.getElementById('agent-log-empty').classList.add('hidden');
  document.getElementById('agent-log').classList.remove('hidden');
}

// ── Agent log rendering ────────────────────────────────────────────────────
function clearAgentLog() {
  const log = document.getElementById('agent-log');
  log.innerHTML = '';
  log.classList.add('hidden');
  document.getElementById('agent-log-empty').classList.remove('hidden');
}

// Spinner timers: node_name -> intervalId
const _spinnerTimers = {};

function addNodeSpinner(node, timestamp) {
  const log = document.getElementById('agent-log');
  showAgentLog();
  // Remove any existing spinner for this node
  const existing = document.getElementById(`spinner-${node}`);
  if (existing) existing.remove();

  const meta = NODE_META[node] || { icon: '⚙', label: node, desc: '' };
  const startTime = timestamp ? new Date(timestamp) : new Date();

  const card = document.createElement('div');
  card.className = 'node-card running card p-3 pl-4 fade-in';
  card.id = `spinner-${node}`;
  card.innerHTML = `
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-2">
        <span class="text-base">${meta.icon}</span>
        <div>
          <span class="text-sm font-medium text-white">${meta.label}</span>
          <span class="text-xs text-gray-500 ml-2">${meta.desc}</span>
        </div>
      </div>
      <div class="flex items-center gap-2 flex-shrink-0">
        <span id="elapsed-${node}" class="text-xs text-gray-600">0s</span>
        <span class="spinner text-yellow-400">↻</span>
      </div>
    </div>`;
  log.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  // Start elapsed timer
  if (_spinnerTimers[node]) clearInterval(_spinnerTimers[node]);
  _spinnerTimers[node] = setInterval(() => {
    const el = document.getElementById(`elapsed-${node}`);
    if (!el) { clearInterval(_spinnerTimers[node]); return; }
    const secs = Math.round((Date.now() - startTime) / 1000);
    el.textContent = secs >= 60 ? `${Math.floor(secs/60)}m${secs%60}s` : `${secs}s`;
  }, 1000);
}

function addPlanDoneCard() {
  const log = document.getElementById('agent-log');
  const card = document.createElement('div');
  card.className = 'card p-3 fade-in border-blue-800 bg-blue-950/20';
  card.innerHTML = `
    <div class="flex items-center gap-2">
      <span class="text-blue-400 text-lg">📋</span>
      <div>
        <p class="text-sm font-medium text-blue-400">Plan ready!</p>
        <p class="text-xs text-gray-500">Review and edit the storyboard, then click Approve &amp; Generate →</p>
      </div>
    </div>`;
  log.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function addNodeCard(node, data, stdout, timestamp) {
  const log = document.getElementById('agent-log');
  const meta = NODE_META[node] || { icon: '⚙', label: node, desc: '' };
  const summary = getNodeSummary(node, data || {});
  const ts = timestamp ? new Date(timestamp).toLocaleTimeString() : '';

  // Remove spinner for this node and stop its timer
  const spinner = document.getElementById(`spinner-${node}`);
  if (spinner) spinner.remove();
  if (_spinnerTimers[node]) { clearInterval(_spinnerTimers[node]); delete _spinnerTimers[node]; }

  // Remove any existing "running" card for this node
  const existing = document.getElementById(`node-${node}`);
  if (existing) existing.remove();

  const cardId = `node-${node}-${Date.now()}`;
  const stdoutHtml = stdout ? `
    <div class="mt-2">
      <button onclick="toggleStdout('${cardId}')" class="text-xs text-gray-500 hover:text-gray-400">
        ▸ Show output
      </button>
      <pre id="${cardId}-stdout" class="hidden log-code mt-1 p-2 rounded text-xs overflow-x-auto whitespace-pre-wrap max-h-48 overflow-y-auto">${escHtml(stdout)}</pre>
    </div>` : '';

  // Key data fields (show non-empty, exclude verbose ones)
  const keyFields = getKeyFields(node, data);
  const fieldsHtml = keyFields.length ? `
    <div class="mt-2 flex flex-wrap gap-2">
      ${keyFields.map(([k, v]) => `
        <span class="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded font-mono">${k}: ${escHtml(String(v))}</span>
      `).join('')}
    </div>` : '';

  const card = document.createElement('div');
  card.className = 'node-card done card p-3 pl-4 fade-in';
  card.id = cardId;
  card.innerHTML = `
    <div class="flex items-start justify-between gap-2">
      <div class="flex items-center gap-2">
        <span class="text-base">${meta.icon}</span>
        <div>
          <span class="text-sm font-medium text-white">${meta.label}</span>
          <span class="text-xs text-gray-500 ml-2">${meta.desc}</span>
        </div>
      </div>
      <span class="text-xs text-gray-600 flex-shrink-0">${ts}</span>
    </div>
    ${summary ? `<p class="text-xs text-gray-400 mt-1.5 ml-7">${escHtml(summary)}</p>` : ''}
    ${fieldsHtml}
    ${stdoutHtml}
  `;
  log.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function getKeyFields(node, data) {
  const fields = [];
  try {
    switch (node) {
      case 'planner_llm': {
        const plan = data.plan || {};
        if (plan.platform) fields.push(['platform', plan.platform]);
        if (plan.duration_sec) fields.push(['duration', plan.duration_sec + 's']);
        if (plan.language) fields.push(['lang', plan.language]);
        const shots = (plan.shot_list || []).length;
        if (shots) fields.push(['shots', shots]);
        break;
      }
      case 'executor_pipeline': {
        const clips = data.scene_clips || [];
        if (clips.length) fields.push(['clips', clips.length]);
        break;
      }
      case 'caption_agent': {
        const segs = data.caption_segments || [];
        if (segs.length) fields.push(['segments', segs.length]);
        break;
      }
      case 'quality_gate': {
        const qr = data.quality_result || {};
        fields.push(['passed', qr.passed ? 'yes' : 'no']);
        if (qr.auto_fix_applied) fields.push(['auto_fix', 'yes']);
        fields.push(['attempt', data.qc_attempt || 1]);
        break;
      }
      case 'render_export': {
        if (data.output_path) {
          const name = data.output_path.split('/').pop();
          fields.push(['file', name]);
        }
        break;
      }
    }
  } catch (e) {}
  return fields;
}

function addQcDiagnoseAlert(message, diagnosis) {
  const log = document.getElementById('agent-log');
  const card = document.createElement('div');
  const isKeyIssue = diagnosis === 'missing_fal_key' || diagnosis === 'missing_anthropic_key';
  card.className = 'card p-4 fade-in border-red-700 bg-red-950/30';
  const actionBtn = isKeyIssue
    ? `<button onclick="openSettings()" class="mt-3 btn-primary text-sm px-4 py-2 rounded-md">
        ⚙️ 打开 API Settings
       </button>`
    : `<button onclick="setAppState('idle')" class="mt-3 btn-secondary text-sm px-4 py-2 rounded-md">
        ↻ 重新描述
       </button>`;
  // Convert markdown **bold** to <strong>
  const html = escHtml(message).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
  card.innerHTML = `
    <div class="flex items-start gap-2">
      <span class="text-red-400 text-lg mt-0.5">⚠️</span>
      <div class="flex-1">
        <p class="text-sm font-semibold text-red-400 mb-1">质量检测失败 — 需要处理</p>
        <p class="text-sm text-gray-300 leading-relaxed">${html}</p>
        ${actionBtn}
      </div>
    </div>`;
  log.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function addDoneCard(projectId) {
  const log = document.getElementById('agent-log');
  const card = document.createElement('div');
  card.className = 'card p-3 fade-in border-green-800 bg-green-950/20';
  card.innerHTML = `
    <div class="flex items-center gap-2">
      <span class="text-green-400 text-lg">🎉</span>
      <div>
        <p class="text-sm font-medium text-green-400">Pipeline complete!</p>
        <p class="text-xs text-gray-500">Video generated. Check the panel on the right →</p>
      </div>
    </div>`;
  log.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  // Load video for this project
  loadProjectVideo(projectId);
}

async function loadProjectVideo(projectId) {
  try {
    const proj = await api('GET', `/api/projects/${projectId}`);
    const paths = proj.output_paths || [];
    if (!paths.length) return;

    document.getElementById('video-empty').classList.add('hidden');
    const container = document.getElementById('video-outputs');
    container.classList.remove('hidden');
    container.innerHTML = '';

    // Show most recent video only (last path)
    const p = paths[paths.length - 1];
    const filename = p.split('/').pop();
    const videoUrl = `/video/${filename}`;
    const isTurbo = proj.latest_plan_json?._quality === 'turbo';

    const div = document.createElement('div');
    div.className = 'fade-in space-y-2';
    div.innerHTML = `
      ${isTurbo ? `<div class="text-xs text-yellow-500 text-center px-1">⚡ Turbo preview (480p · ~2s clips)</div>` : `<div class="text-xs text-blue-400 text-center px-1">✦ HD quality (720p · ~5s clips)</div>`}
      <video controls playsinline class="w-full rounded-lg border border-gray-700 bg-black"
        style="max-height: 480px; aspect-ratio: 9/16; object-fit: contain;">
        <source src="${videoUrl}" type="video/mp4"/>
      </video>
      ${isTurbo ? `
      <button onclick="upgradeToHD()"
        class="btn-approve w-full text-sm py-2 rounded-md font-medium flex items-center justify-center gap-2">
        ✦ Upgrade to HD
      </button>` : ''}
      <a href="${videoUrl}" download="${filename}"
        class="flex items-center justify-center gap-1.5 btn-secondary text-xs py-1.5 rounded-md w-full">
        ⬇ Download
      </a>`;
    container.appendChild(div);
  } catch (e) {}
}

function addErrorCard(message, tb) {
  const log = document.getElementById('agent-log');
  const card = document.createElement('div');
  card.className = 'node-card error card p-3 pl-4 fade-in';
  const tbHtml = tb ? `
    <button onclick="this.nextElementSibling.classList.toggle('hidden')" class="text-xs text-red-400 mt-1 hover:underline">
      ▸ Show traceback
    </button>
    <pre class="hidden log-code mt-1 p-2 rounded text-xs overflow-x-auto whitespace-pre-wrap max-h-64 text-red-300">${escHtml(tb)}</pre>` : '';
  card.innerHTML = `
    <div class="flex items-center gap-2 mb-1">
      <span>❌</span>
      <span class="text-sm font-medium text-red-400">Pipeline error</span>
    </div>
    <p class="text-xs text-red-300 ml-6">${escHtml(message)}</p>
    ${tbHtml}`;
  log.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function toggleStdout(cardId) {
  const el = document.getElementById(cardId + '-stdout');
  const btn = el.previousElementSibling;
  if (el.classList.contains('hidden')) {
    el.classList.remove('hidden');
    btn.textContent = '▾ Hide output';
  } else {
    el.classList.add('hidden');
    btn.textContent = '▸ Show output';
  }
}

function escHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// (feedback is now handled via chat input — submitFeedback(text) above)

// ── Init DB ────────────────────────────────────────────────────────────────
async function initDb() {
  try {
    const res = await api('POST', '/api/init');
    toast(res.message, 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.id === 'chat-input' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    handleChatSend();
  }
  if (e.key === 'Enter' && (e.target.id === 'api-key-input' || e.target.id === 'fal-key-input')) {
    saveApiKey();
  }
  if (e.key === 'Escape') {
    closeSettings();
  }
});

// ── Tabs ───────────────────────────────────────────────────────────────────
function switchTab(name) {
  const tabs = ['log', 'plan'];
  tabs.forEach(t => {
    document.getElementById(`pane-${t}`).classList.toggle('hidden', t !== name);
    const btn = document.getElementById(`tab-${t}`);
    if (t === name) {
      btn.className = 'tab-btn text-xs py-2.5 px-3 border-b-2 border-blue-500 text-blue-400 font-medium';
    } else {
      btn.className = 'tab-btn text-xs py-2.5 px-3 border-b-2 border-transparent text-gray-500 hover:text-gray-400';
    }
  });
  if (name === 'plan' && currentPlan) {
    renderPlan(currentPlan, appState === 'plan_ready');
  } else if (name === 'plan') {
    loadPlanView();
  }
}

async function loadPlanView() {
  if (!currentProjectId) return;
  try {
    const proj = await api('GET', `/api/projects/${currentProjectId}`);
    if (proj.latest_plan_json) {
      currentPlan = proj.latest_plan_json;
      renderPlan(currentPlan, appState === 'plan_ready');
    }
  } catch (e) {}
}

function renderPlan(plan, editable) {
  const empty = document.getElementById('plan-empty');
  const content = document.getElementById('plan-content');

  if (!plan || !plan.script) {
    empty.classList.remove('hidden');
    content.classList.add('hidden');
    return;
  }

  empty.classList.add('hidden');
  content.classList.remove('hidden');

  const assetIcon = { macro: '🔬', product: '📦', lifestyle: '🌿', close: '🔍', wide: '🌅', text: '📝', transition: '✨' };
  const toneColors = { fresh: 'bg-green-900 text-green-300', playful: 'bg-yellow-900 text-yellow-300',
    premium: 'bg-purple-900 text-purple-300', strong_promo: 'bg-red-900 text-red-300',
    promo: 'bg-red-900 text-red-300', funny: 'bg-orange-900 text-orange-300' };

  const tones = (plan.style_tone || []).map(t =>
    `<span class="text-xs px-2 py-0.5 rounded-full ${toneColors[t] || 'bg-gray-800 text-gray-400'}">${t}</span>`
  ).join(' ');

  const bodyLines = (plan.script.body || []).map((l, i) =>
    `<p class="text-sm text-gray-300 py-1 border-b border-gray-800 last:border-0">${i + 1}. ${escHtml(l)}</p>`
  ).join('');

  const storyboard = (plan.storyboard || []).map((scene, i) => {
    const shot = (plan.shot_list || [])[i] || {};
    if (editable) {
      return `
      <div class="card p-3 fade-in" data-scene-idx="${i}">
        <div class="flex gap-2 mb-2 items-center flex-wrap">
          <span class="text-xs font-bold text-gray-400">Scene ${scene.scene || i+1}</span>
          <span class="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded font-mono">${assetIcon[scene.asset_hint] || '🎬'} ${scene.asset_hint || '?'}</span>
          <input type="number" data-field="duration" value="${scene.duration || 3}" min="1" max="30" step="0.5"
            class="w-14 text-xs p-1 rounded" style="background:#0d1117;border:1px solid #30363d"/>
          <span class="text-xs text-gray-500">s</span>
        </div>
        <textarea data-field="desc" rows="3"
          class="w-full text-sm p-2 resize-none rounded mb-1"
          style="background:#0d1117;border:1px solid #30363d">${escHtml(scene.desc)}</textarea>
        <input data-field="overlay" type="text" value="${escHtml(shot.text_overlay || '')}"
          placeholder="Text overlay (optional)"
          class="w-full text-xs p-1.5 rounded" style="background:#0d1117;border:1px solid #30363d"/>
      </div>`;
    } else {
      return `
      <div class="card p-3 fade-in">
        <div class="flex items-start gap-3">
          <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-800 flex items-center justify-center text-sm font-bold text-gray-400">${scene.scene || i+1}</div>
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1.5 flex-wrap">
              <span class="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded font-mono">${assetIcon[scene.asset_hint] || '🎬'} ${scene.asset_hint || '?'}</span>
              <span class="text-xs text-gray-600">${scene.duration}s</span>
            </div>
            <p class="text-sm text-gray-300 leading-relaxed">${escHtml(scene.desc)}</p>
            ${shot.text_overlay ? `
              <div class="mt-2 flex items-start gap-1.5">
                <span class="text-xs text-gray-600 flex-shrink-0 mt-0.5">overlay:</span>
                <span class="text-xs text-yellow-400 font-mono">"${escHtml(shot.text_overlay)}"</span>
              </div>` : ''}
          </div>
        </div>
      </div>`;
    }
  }).join('');

  const totalDuration = (plan.storyboard || []).reduce((s, sc) => s + (sc.duration || 0), 0).toFixed(1);

  content.innerHTML = `
    <!-- Meta -->
    <div class="card p-3">
      <div class="flex flex-wrap gap-2 items-center">
        <span class="text-xs text-gray-500">Platform:</span>
        <span class="text-xs text-white font-medium">${escHtml(plan.platform || '?')}</span>
        <span class="text-gray-700">·</span>
        <span class="text-xs text-gray-500">Duration:</span>
        <span class="text-xs text-white font-medium">${plan.duration_sec}s (actual: ${totalDuration}s)</span>
        <span class="text-gray-700">·</span>
        <span class="text-xs text-gray-500">Lang:</span>
        <span class="text-xs text-white font-medium">${escHtml(plan.language || '?')}</span>
        <span class="text-gray-700">·</span>
        ${tones}
      </div>
    </div>

    <!-- Script -->
    <div>
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Script</h3>
      <div class="card p-3 space-y-3">
        <div>
          <span class="text-xs text-yellow-500 font-semibold uppercase tracking-wider">Hook</span>
          <p class="text-base font-medium text-white mt-1">${escHtml(plan.script.hook || '')}</p>
        </div>
        <div>
          <span class="text-xs text-blue-400 font-semibold uppercase tracking-wider">Body</span>
          <div class="mt-1">${bodyLines}</div>
        </div>
        <div>
          <span class="text-xs text-green-500 font-semibold uppercase tracking-wider">CTA</span>
          <p class="text-sm text-green-300 mt-1 font-medium">${escHtml(plan.script.cta || '')}</p>
        </div>
      </div>
    </div>

    <!-- Storyboard -->
    <div>
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
        Storyboard · ${(plan.storyboard || []).length} scenes${editable ? ' — <span class="text-blue-400 normal-case font-normal">editable</span>' : ''}
      </h3>
      <div class="space-y-2">${storyboard}</div>
    </div>
  `;
}

// ── Settings / API Key ─────────────────────────────────────────────────────
async function openSettings() {
  try {
    const s = await api('GET', '/api/settings');
    const cur = document.getElementById('api-key-current');
    if (s.anthropic_api_key_set) {
      cur.textContent = `Current: ${s.anthropic_api_key_preview}`;
      cur.className = 'text-xs text-green-600 mt-1.5';
    } else {
      cur.textContent = 'Not set — mock planner will be used';
      cur.className = 'text-xs text-yellow-600 mt-1.5';
    }
    const falCur = document.getElementById('fal-key-current');
    if (s.fal_key_set) {
      falCur.textContent = `Current: ${s.fal_key_preview}`;
      falCur.className = 'text-xs text-green-600 mt-1.5';
    } else {
      falCur.textContent = 'Not set — PIL placeholder clips will be used';
      falCur.className = 'text-xs text-yellow-600 mt-1.5';
    }
  } catch (e) {}
  document.getElementById('settings-modal').classList.remove('hidden');
  document.getElementById('api-key-input').focus();
}

function closeSettings() {
  document.getElementById('settings-modal').classList.add('hidden');
  document.getElementById('api-key-input').value = '';
  document.getElementById('fal-key-input').value = '';
}

function toggleKeyVisibility() {
  const input = document.getElementById('api-key-input');
  input.type = input.type === 'password' ? 'text' : 'password';
}

function toggleFalKeyVisibility() {
  const input = document.getElementById('fal-key-input');
  input.type = input.type === 'password' ? 'text' : 'password';
}

async function saveApiKey() {
  const antKey = document.getElementById('api-key-input').value.trim();
  const falKey = document.getElementById('fal-key-input').value.trim();
  if (!antKey && !falKey) { toast('Please enter at least one API key', 'error'); return; }
  try {
    const res = await api('POST', '/api/settings', { anthropic_api_key: antKey, fal_key: falKey });
    const parts = [];
    if (res.anthropic_preview) parts.push(`Anthropic: ${res.anthropic_preview}`);
    if (res.fal_preview) parts.push(`fal.ai: ${res.fal_preview}`);
    toast(`Saved — ${parts.join(' | ')}`, 'success');
    closeSettings();
    updateApiStatus();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function updateApiStatus() {
  try {
    const s = await api('GET', '/api/settings');
    const el = document.getElementById('api-status');
    if (!s.anthropic_api_key_set) {
      el.classList.remove('hidden');
    } else {
      el.classList.add('hidden');
    }
  } catch (e) {}
}

// ── Brand Kit UI ───────────────────────────────────────────────────────────
let _brandKits = [];

async function loadBrandKits() {
  try {
    _brandKits = await api('GET', '/api/brand-kits');
    renderBrandKitList(_brandKits);
    updateBrandSelect(_brandKits);
  } catch (e) {
    document.getElementById('brand-kit-list').innerHTML =
      `<p class="text-xs text-red-400 text-center py-1">${e.message}</p>`;
  }
}

function renderBrandKitList(kits) {
  const el = document.getElementById('brand-kit-list');
  if (!kits.length) {
    el.innerHTML = '<p class="text-xs text-gray-600 text-center py-1">No brand kits</p>';
    return;
  }
  el.innerHTML = kits.map(k => {
    const primary = k.colors?.primary || '#888';
    const hasLogo = k.logo?.path;
    const logoHtml = hasLogo
      ? `<img src="/api/brand-kits/${k.brand_id}/logo" class="w-5 h-5 rounded object-contain bg-gray-900 border border-gray-700" onerror="this.style.display='none'"/>`
      : `<span class="w-5 h-5 rounded flex items-center justify-center bg-gray-800 text-gray-600 text-xs">?</span>`;
    return `
      <div class="card card-hover p-1.5 flex items-center gap-1.5">
        ${logoHtml}
        <span class="flex-1 text-xs text-gray-300 truncate">${escHtml(k.name || k.brand_id)}</span>
        <span class="w-3 h-3 rounded-full flex-shrink-0" style="background:${primary}" title="${primary}"></span>
        <button onclick="openEditBrandKitModal('${k.brand_id}')"
          class="text-xs text-gray-500 hover:text-blue-400 px-1 flex-shrink-0">Edit</button>
      </div>`;
  }).join('');
}

function updateBrandSelect(kits) {
  const sel = document.getElementById('brand-select');
  const cur = sel.value;
  sel.innerHTML = kits.map(k =>
    `<option value="${k.brand_id}">${escHtml(k.name || k.brand_id)}</option>`
  ).join('');
  if (kits.find(k => k.brand_id === cur)) sel.value = cur;
  selectedBrandId = sel.value;
}

function openNewBrandKitModal() {
  document.getElementById('bk-modal-title').textContent = 'New Brand Kit';
  document.getElementById('bk-id').value = '';
  document.getElementById('bk-name').value = '';
  document.getElementById('bk-color-primary').value = '#00B894';
  document.getElementById('bk-color-primary-hex').value = '#00B894';
  document.getElementById('bk-color-secondary').value = '#FFFFFF';
  document.getElementById('bk-color-secondary-hex').value = '#FFFFFF';
  document.getElementById('bk-color-accent').value = '#FF7675';
  document.getElementById('bk-color-accent-hex').value = '#FF7675';
  document.getElementById('bk-color-background').value = '#1A1A2E';
  document.getElementById('bk-color-background-hex').value = '#1A1A2E';
  document.getElementById('bk-safe-area').value = 'top_right';
  document.getElementById('bk-logo-section').classList.add('hidden');
  document.getElementById('bk-logo-preview').classList.add('hidden');
  document.getElementById('bk-logo-url').value = '';
  document.getElementById('bk-delete-btn').classList.add('hidden');
  document.getElementById('brand-kit-modal').classList.remove('hidden');
}

function openEditBrandKitModal(brandId) {
  const kit = _brandKits.find(k => k.brand_id === brandId);
  if (!kit) return;
  document.getElementById('bk-modal-title').textContent = 'Edit Brand Kit';
  document.getElementById('bk-id').value = kit.brand_id;
  document.getElementById('bk-name').value = kit.name || '';
  const c = kit.colors || {};
  ['primary', 'secondary', 'accent', 'background'].forEach(k => {
    const val = c[k] || '#888888';
    document.getElementById(`bk-color-${k}`).value = val;
    document.getElementById(`bk-color-${k}-hex`).value = val;
  });
  document.getElementById('bk-safe-area').value = kit.logo?.safe_area || 'top_right';
  document.getElementById('bk-logo-section').classList.remove('hidden');
  document.getElementById('bk-logo-url').value = '';
  // Show logo preview if exists
  const preview = document.getElementById('bk-logo-preview');
  const img = document.getElementById('bk-logo-img');
  if (kit.logo?.path) {
    img.src = `/api/brand-kits/${kit.brand_id}/logo?t=${Date.now()}`;
    preview.classList.remove('hidden');
  } else {
    preview.classList.add('hidden');
  }
  document.getElementById('bk-delete-btn').classList.remove('hidden');
  document.getElementById('brand-kit-modal').classList.remove('hidden');
}

function closeBrandKitModal() {
  document.getElementById('brand-kit-modal').classList.add('hidden');
}

function _getBkColors() {
  return {
    primary: document.getElementById('bk-color-primary-hex').value || document.getElementById('bk-color-primary').value,
    secondary: document.getElementById('bk-color-secondary-hex').value || document.getElementById('bk-color-secondary').value,
    accent: document.getElementById('bk-color-accent-hex').value || document.getElementById('bk-color-accent').value,
    background: document.getElementById('bk-color-background-hex').value || document.getElementById('bk-color-background').value,
  };
}

async function saveBrandKit() {
  const brandId = document.getElementById('bk-id').value;
  const name = document.getElementById('bk-name').value.trim();
  const safe_area = document.getElementById('bk-safe-area').value;
  const colors = _getBkColors();
  if (!name) { toast('Brand name is required', 'error'); return; }
  try {
    if (brandId) {
      await api('PUT', `/api/brand-kits/${brandId}`, { name, colors, safe_area });
      toast('Brand kit saved', 'success');
    } else {
      await api('POST', '/api/brand-kits', { name, colors, safe_area });
      toast('Brand kit created', 'success');
    }
    closeBrandKitModal();
    loadBrandKits();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function deleteBrandKit() {
  const brandId = document.getElementById('bk-id').value;
  if (!brandId) return;
  if (!confirm('Delete this brand kit?')) return;
  try {
    await api('DELETE', `/api/brand-kits/${brandId}`);
    toast('Brand kit deleted', 'success');
    closeBrandKitModal();
    loadBrandKits();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function fetchLogoFromUrl() {
  const brandId = document.getElementById('bk-id').value;
  const url = document.getElementById('bk-logo-url').value.trim();
  if (!brandId) { toast('Save the brand kit first before fetching a logo', 'error'); return; }
  if (!url) { toast('Enter a website URL', 'error'); return; }
  try {
    await api('POST', '/api/brand-kits/fetch-logo', { url, brand_id: brandId });
    toast('Logo fetched!', 'success');
    const img = document.getElementById('bk-logo-img');
    img.src = `/api/brand-kits/${brandId}/logo?t=${Date.now()}`;
    document.getElementById('bk-logo-preview').classList.remove('hidden');
    loadBrandKits();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function uploadLogoFile(input) {
  const brandId = document.getElementById('bk-id').value;
  if (!brandId) { toast('Save the brand kit first before uploading a logo', 'error'); return; }
  const file = input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await fetch(`/api/brand-kits/${brandId}/logo`, { method: 'POST', body: formData });
    if (!res.ok) { const e = await res.json(); throw new Error(e.detail); }
    toast('Logo uploaded!', 'success');
    const img = document.getElementById('bk-logo-img');
    img.src = `/api/brand-kits/${brandId}/logo?t=${Date.now()}`;
    document.getElementById('bk-logo-preview').classList.remove('hidden');
    loadBrandKits();
  } catch (e) {
    toast(e.message || 'Upload failed', 'error');
  }
}

// Sync color pickers ↔ hex inputs
['primary', 'secondary', 'accent', 'background'].forEach(k => {
  const picker = document.getElementById(`bk-color-${k}`);
  const hex = document.getElementById(`bk-color-${k}-hex`);
  if (picker && hex) {
    picker.addEventListener('input', () => { hex.value = picker.value; });
    hex.addEventListener('input', () => {
      if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) picker.value = hex.value;
    });
  }
});

// ── Init ───────────────────────────────────────────────────────────────────
setAppState('idle');
loadProjects();
loadBrandKits();
updateApiStatus();
document.getElementById('chat-input').focus();
// Refresh project list every 10s
setInterval(loadProjects, 10000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
