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

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import agent.deps as deps

app = FastAPI(title="Video Agent Hero")

# ── In-memory SSE state ───────────────────────────────────────────────────────

# Active queues for ongoing runs
_run_queues: dict[str, asyncio.Queue] = {}
# Stored events for completed/replayed runs
_run_events: dict[str, list[dict]] = {}


# ── Startup ───────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    deps.init()


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
    return proj


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


@app.post("/api/projects/{project_id}/feedback")
async def submit_feedback(project_id: str, req: FeedbackRequest, background_tasks: BackgroundTasks):
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    deps.db().add_feedback(project_id, req.text, req.rating)

    if not req.replan:
        return {"status": "saved"}

    from agent.graph import build_replan_graph

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
    deps.db().update_project_status(project_id, "running")
    background_tasks.add_task(
        _run_agent_with_state, project_id=project_id,
        initial_state=replan_state, queue=queue, replan=True,
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

    await _run_agent_with_state(project_id, initial_state, queue, replan=False)


async def _run_agent_with_state(
    project_id: str, initial_state: dict, queue: asyncio.Queue, replan: bool
):
    loop = asyncio.get_running_loop()

    def _emit(event: dict):
        _run_events.setdefault(project_id, []).append(event)
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_in_thread():
        from agent.graph import build_graph, build_replan_graph

        graph = build_replan_graph() if replan else build_graph()
        stdout_buf = io.StringIO()

        try:
            node_start: dict[str, str] = {}

            with contextlib.redirect_stdout(stdout_buf):
                for chunk in graph.stream(initial_state, stream_mode="updates"):
                    captured = _strip_ansi(stdout_buf.getvalue()).strip()
                    stdout_buf.truncate(0)
                    stdout_buf.seek(0)

                    for node_name, node_output in chunk.items():
                        ts = datetime.now().isoformat()
                        started = node_start.get(node_name, ts)
                        _emit({
                            "type": "node_done",
                            "node": node_name,
                            "data": _serialize(node_output),
                            "stdout": captured,
                            "timestamp": ts,
                            "started_at": started,
                        })
                        # Mark next nodes as started (heuristic)
                        node_start[node_name] = ts

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

<div class="flex flex-1 overflow-hidden">

<!-- Sidebar -->
<aside class="sidebar w-64 flex-shrink-0 flex flex-col overflow-hidden">
  <div class="p-3 border-b border-gray-800">
    <button onclick="showNewForm()" class="btn-primary w-full text-sm py-2 rounded-md font-medium">
      + New Project
    </button>
  </div>

  <!-- New project form -->
  <div id="new-form" class="hidden p-3 border-b border-gray-800">
    <div class="mb-2">
      <label class="text-xs text-gray-400 mb-1 block">Brief</label>
      <textarea id="new-brief" rows="3" class="w-full text-sm p-2 resize-none"
        placeholder="Describe the video you want to create..."></textarea>
    </div>
    <div class="grid grid-cols-2 gap-2 mb-2">
      <div>
        <label class="text-xs text-gray-400 mb-1 block">Brand</label>
        <input id="new-brand" type="text" value="tong_sui" class="w-full text-xs p-1.5"/>
      </div>
      <div>
        <label class="text-xs text-gray-400 mb-1 block">User</label>
        <input id="new-user" type="text" value="ej" class="w-full text-xs p-1.5"/>
      </div>
    </div>
    <div class="flex gap-2">
      <button onclick="createProject()" class="btn-primary text-xs px-3 py-1.5 rounded-md flex-1">Create</button>
      <button onclick="hideNewForm()" class="btn-secondary text-xs px-3 py-1.5 rounded-md">Cancel</button>
    </div>
  </div>

  <!-- Project list -->
  <div id="project-list" class="flex-1 overflow-y-auto p-2 space-y-1">
    <p class="text-xs text-gray-500 text-center pt-4">Loading projects...</p>
  </div>
</aside>

<!-- Main content -->
<main class="flex-1 overflow-hidden flex flex-col">
  <!-- Empty state -->
  <div id="empty-state" class="flex-1 flex items-center justify-center text-gray-600">
    <div class="text-center">
      <div class="text-5xl mb-4">🎬</div>
      <p class="text-lg font-medium text-gray-500">Select or create a project</p>
      <p class="text-sm mt-1">Run the AI agent to generate your video</p>
    </div>
  </div>

  <!-- Project view -->
  <div id="project-view" class="hidden flex-1 flex flex-col overflow-hidden">
    <!-- Project header -->
    <div class="p-4 border-b border-gray-800 flex-shrink-0" style="min-height:0">
      <div class="flex items-start justify-between gap-4">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-1">
            <span id="proj-status-dot" class="text-xs font-mono px-2 py-0.5 rounded-full bg-gray-800"></span>
            <span id="proj-id" class="text-xs text-gray-500 font-mono"></span>
          </div>
          <p id="proj-brief" class="text-sm text-gray-300 leading-relaxed"></p>
          <div class="flex gap-3 mt-2 text-xs text-gray-500">
            <span>Brand: <span id="proj-brand" class="text-gray-400"></span></span>
            <span>User: <span id="proj-user" class="text-gray-400"></span></span>
            <span>Created: <span id="proj-created" class="text-gray-400"></span></span>
          </div>
        </div>
        <div class="flex gap-2 flex-shrink-0">
          <button onclick="runProject()" id="run-btn"
            class="btn-primary text-sm px-4 py-2 rounded-md font-medium flex items-center gap-2">
            <span>▶</span> Run Pipeline
          </button>
          <button onclick="showFeedbackForm()" id="feedback-btn"
            class="btn-secondary text-sm px-3 py-2 rounded-md hidden">
            ✎ Feedback
          </button>
        </div>
      </div>

      <!-- Feedback form -->
      <div id="feedback-form" class="hidden mt-3 pt-3 border-t border-gray-800">
        <div class="flex gap-2">
          <input id="feedback-text" type="text" class="flex-1 text-sm p-2"
            placeholder="e.g. Make it more energetic, change the color to blue..."/>
          <select id="feedback-rating" class="text-sm p-2">
            <option value="">Rating</option>
            <option value="5">⭐⭐⭐⭐⭐ 5</option>
            <option value="4">⭐⭐⭐⭐ 4</option>
            <option value="3">⭐⭐⭐ 3</option>
            <option value="2">⭐⭐ 2</option>
            <option value="1">⭐ 1</option>
          </select>
          <button onclick="submitFeedback()"
            class="btn-primary text-sm px-3 py-2 rounded-md">Replan</button>
          <button onclick="hideFeedbackForm()"
            class="btn-secondary text-sm px-3 py-2 rounded-md">Cancel</button>
        </div>
      </div>
    </div>

    <!-- Body: agent log + video player -->
    <div class="flex flex-1 overflow-hidden">

      <!-- Agent log + Plan tabs -->
      <div class="flex-1 flex flex-col overflow-hidden border-r border-gray-800">
        <!-- Tab bar -->
        <div class="flex border-b border-gray-800 px-4 flex-shrink-0">
          <button onclick="switchTab('log')" id="tab-log"
            class="tab-btn text-xs py-2.5 px-3 border-b-2 border-blue-500 text-blue-400 font-medium">
            Agent Log
          </button>
          <button onclick="switchTab('plan')" id="tab-plan"
            class="tab-btn text-xs py-2.5 px-3 border-b-2 border-transparent text-gray-500 hover:text-gray-400">
            Storyboard &amp; Plan
          </button>
        </div>

        <!-- Log pane -->
        <div id="pane-log" class="flex-1 overflow-y-auto p-4">
          <div id="agent-log-empty" class="text-center text-gray-600 py-12">
            <p class="text-sm">Click <strong class="text-gray-500">▶ Run Pipeline</strong> to start the agent</p>
          </div>
          <div id="agent-log" class="space-y-3 hidden"></div>
        </div>

        <!-- Plan pane -->
        <div id="pane-plan" class="hidden flex-1 overflow-y-auto p-4">
          <div id="plan-empty" class="text-center text-gray-600 py-12">
            <p class="text-sm">No plan yet — run the pipeline first</p>
          </div>
          <div id="plan-content" class="hidden space-y-5"></div>
        </div>
      </div>

      <!-- Video panel -->
      <div id="video-panel" class="w-64 flex-shrink-0 flex flex-col p-4 gap-3 overflow-y-auto">
        <div id="video-empty" class="flex-1 flex flex-col items-center justify-center text-gray-700 gap-2">
          <span class="text-4xl">🎞</span>
          <p class="text-xs text-center">Output video will appear here after the pipeline completes</p>
        </div>
        <div id="video-outputs" class="hidden space-y-3"></div>
      </div>

    </div>
  </div>
</main>

</div>

<!-- Toast -->
<div id="toast" class="fixed bottom-4 right-4 hidden">
  <div class="bg-gray-800 border border-gray-700 text-sm px-4 py-2 rounded-lg shadow-lg"></div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentProjectId = null;
let eventSource = null;
let nodeStartTimes = {};

// ── Node metadata ──────────────────────────────────────────────────────────
const NODE_META = {
  intent_parser:        { icon: '🔍', label: 'Intent Parser',       desc: 'Extracting hints from brief' },
  memory_loader:        { icon: '🧠', label: 'Memory Loader',       desc: 'Loading brand kit & preferences' },
  clarification_planner:{ icon: '❓', label: 'Clarification Planner', desc: 'Checking required fields' },
  ask_user:             { icon: '💬', label: 'Ask User',            desc: 'Collecting clarification answers' },
  planner_llm:          { icon: '✨', label: 'LLM Planner',         desc: 'Generating video plan with AI' },
  plan_checker:         { icon: '✅', label: 'Plan Checker',        desc: 'Validating & fixing plan' },
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

// ── Project list ───────────────────────────────────────────────────────────
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
      done: 'status-done', running: 'status-running',
      failed: 'status-failed'
    }[p.status] || 'status-pending';
    const dot = { done: '●', running: '◉', failed: '✕', pending: '○' }[p.status] || '○';
    const brief = p.brief.length > 55 ? p.brief.slice(0, 55) + '…' : p.brief;
    const active = p.project_id === currentProjectId ? 'border-blue-500 bg-blue-950/20' : '';
    return `
      <div onclick="selectProject('${p.project_id}')"
        class="card card-hover p-2.5 ${active} transition-colors">
        <div class="flex items-center gap-2 mb-1">
          <span class="${statusClass} text-xs">${dot}</span>
          <span class="text-xs text-gray-500 font-mono truncate">${p.project_id.slice(0, 8)}</span>
          <span class="text-xs text-gray-600 ml-auto">${p.status}</span>
        </div>
        <p class="text-xs text-gray-400 leading-relaxed">${brief}</p>
        <p class="text-xs text-gray-600 mt-1">${p.brand_id} · ${p.created_at?.slice(0, 10) || ''}</p>
      </div>`;
  }).join('');
}

async function selectProject(id) {
  currentProjectId = id;
  try {
    const proj = await api('GET', `/api/projects/${id}`);
    showProjectView(proj);
    // Re-render list to highlight selected
    loadProjects();
    // Reset video panel
    document.getElementById('video-empty').classList.remove('hidden');
    document.getElementById('video-outputs').classList.add('hidden');
    document.getElementById('video-outputs').innerHTML = '';

    // Check if there are stored events to replay
    if (_run_events_cache[id]) {
      clearAgentLog();
      replayEvents(_run_events_cache[id]);
    } else {
      clearAgentLog();
      // Show feedback button if done
      const fbBtn = document.getElementById('feedback-btn');
      if (proj.status === 'done') fbBtn.classList.remove('hidden');
      else fbBtn.classList.add('hidden');
    }

    // Load video if project is already done
    if (proj.status === 'done' && (proj.output_paths || []).length) {
      loadProjectVideo(id);
    }
  } catch (e) {
    toast(e.message, 'error');
  }
}

const _run_events_cache = {}; // project_id -> events[]

function showProjectView(proj) {
  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('project-view').classList.remove('hidden');

  const statusLabels = { done: '✓ done', running: '⟳ running', failed: '✕ failed', pending: '● pending' };
  const statusColors = { done: 'text-green-400', running: 'text-yellow-400', failed: 'text-red-400', pending: 'text-gray-400' };
  const dot = document.getElementById('proj-status-dot');
  dot.textContent = statusLabels[proj.status] || proj.status;
  dot.className = `text-xs font-mono px-2 py-0.5 rounded-full bg-gray-800 ${statusColors[proj.status] || ''}`;

  document.getElementById('proj-id').textContent = proj.project_id;
  document.getElementById('proj-brief').textContent = proj.brief;
  document.getElementById('proj-brand').textContent = proj.brand_id;
  document.getElementById('proj-user').textContent = proj.user_id;
  document.getElementById('proj-created').textContent = proj.created_at?.slice(0, 19).replace('T', ' ') || '—';

  const fbBtn = document.getElementById('feedback-btn');
  if (proj.status === 'done') fbBtn.classList.remove('hidden');
  else fbBtn.classList.add('hidden');
}

// ── New project form ───────────────────────────────────────────────────────
function showNewForm() {
  document.getElementById('new-form').classList.remove('hidden');
  document.getElementById('new-brief').focus();
}
function hideNewForm() {
  document.getElementById('new-form').classList.add('hidden');
}

async function createProject() {
  const brief = document.getElementById('new-brief').value.trim();
  if (!brief) { toast('Please enter a brief', 'error'); return; }
  const brand_id = document.getElementById('new-brand').value.trim() || 'tong_sui';
  const user_id = document.getElementById('new-user').value.trim() || 'ej';
  try {
    const res = await api('POST', '/api/projects', { brief, brand_id, user_id });
    hideNewForm();
    document.getElementById('new-brief').value = '';
    toast('Project created!', 'success');
    await loadProjects();
    await selectProject(res.project_id);
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Run pipeline ───────────────────────────────────────────────────────────
async function runProject() {
  if (!currentProjectId) return;

  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner">↻</span> Running...';

  clearAgentLog();
  document.getElementById('agent-log-empty').classList.add('hidden');
  document.getElementById('agent-log').classList.remove('hidden');
  _run_events_cache[currentProjectId] = [];
  nodeStartTimes = {};

  try {
    await api('POST', `/api/projects/${currentProjectId}/run`, { skip_clarification: true });
    connectEventStream(currentProjectId);
  } catch (e) {
    toast(e.message, 'error');
    btn.disabled = false;
    btn.innerHTML = '<span>▶</span> Run Pipeline';
  }
}

function connectEventStream(projectId) {
  if (eventSource) { eventSource.close(); }

  eventSource = new EventSource(`/api/projects/${projectId}/events`);

  eventSource.onmessage = (e) => {
    const event = JSON.parse(e.data);
    _run_events_cache[projectId] = _run_events_cache[projectId] || [];
    _run_events_cache[projectId].push(event);
    handleEvent(event, projectId);
  };

  eventSource.onerror = () => {
    eventSource.close();
    eventSource = null;
    resetRunBtn();
  };
}

function handleEvent(event, projectId) {
  if (event.type === 'node_done') {
    addNodeCard(event.node, event.data, event.stdout, event.timestamp);
    if (event.node === 'planner_llm') {
      if (!document.getElementById('pane-plan').classList.contains('hidden')) {
        loadPlanView();
      }
    }
    if (event.node === 'qc_diagnose' && event.data.needs_user_action) {
      addQcDiagnoseAlert(event.data.qc_user_message, event.data.qc_diagnosis);
      resetRunBtn();
      if (eventSource) { eventSource.close(); eventSource = null; }
      return;
    }
  } else if (event.type === 'done') {
    addDoneCard(projectId);
    resetRunBtn();
    loadProjects();
    // Show feedback button
    document.getElementById('feedback-btn').classList.remove('hidden');
    if (eventSource) { eventSource.close(); eventSource = null; }
  } else if (event.type === 'error') {
    addErrorCard(event.message, event.traceback);
    resetRunBtn();
    if (eventSource) { eventSource.close(); eventSource = null; }
  }
}

function replayEvents(events) {
  document.getElementById('agent-log-empty').classList.add('hidden');
  document.getElementById('agent-log').classList.remove('hidden');
  for (const event of events) {
    handleEvent(event, currentProjectId);
  }
}

function resetRunBtn() {
  const btn = document.getElementById('run-btn');
  btn.disabled = false;
  btn.innerHTML = '<span>▶</span> Run Pipeline';
}

// ── Agent log rendering ────────────────────────────────────────────────────
function clearAgentLog() {
  const log = document.getElementById('agent-log');
  log.innerHTML = '';
  log.classList.add('hidden');
  document.getElementById('agent-log-empty').classList.remove('hidden');
}

function addNodeCard(node, data, stdout, timestamp) {
  const log = document.getElementById('agent-log');
  const meta = NODE_META[node] || { icon: '⚙', label: node, desc: '' };
  const summary = getNodeSummary(node, data || {});
  const ts = timestamp ? new Date(timestamp).toLocaleTimeString() : '';

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
    : `<button onclick="document.getElementById('run-btn').click()" class="mt-3 btn-secondary text-sm px-4 py-2 rounded-md">
        ↻ 重新跑 Pipeline
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

    for (const p of paths) {
      const filename = p.split('/').pop();
      const videoUrl = `/video/${filename}`;
      const div = document.createElement('div');
      div.className = 'fade-in';
      div.innerHTML = `
        <p class="text-xs text-gray-500 mb-1.5 truncate" title="${escHtml(filename)}">${escHtml(filename)}</p>
        <video controls playsinline class="w-full rounded-lg border border-gray-700 bg-black"
          style="max-height: 480px; aspect-ratio: 9/16; object-fit: contain;">
          <source src="${videoUrl}" type="video/mp4"/>
        </video>
        <a href="${videoUrl}" download="${filename}"
          class="mt-2 flex items-center justify-center gap-1.5 btn-secondary text-xs py-1.5 rounded-md w-full">
          ⬇ Download
        </a>`;
      container.appendChild(div);
    }
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

// ── Feedback form ──────────────────────────────────────────────────────────
function showFeedbackForm() {
  document.getElementById('feedback-form').classList.remove('hidden');
  document.getElementById('feedback-text').focus();
}
function hideFeedbackForm() {
  document.getElementById('feedback-form').classList.add('hidden');
}

async function submitFeedback() {
  if (!currentProjectId) return;
  const text = document.getElementById('feedback-text').value.trim();
  if (!text) { toast('Please enter feedback', 'error'); return; }
  const rating = parseInt(document.getElementById('feedback-rating').value) || null;

  try {
    hideFeedbackForm();
    clearAgentLog();
    _run_events_cache[currentProjectId] = [];
    document.getElementById('agent-log').classList.remove('hidden');
    document.getElementById('agent-log-empty').classList.add('hidden');
    document.getElementById('feedback-btn').classList.add('hidden');

    const res = await api('POST', `/api/projects/${currentProjectId}/feedback`, {
      text, rating, replan: true
    });
    document.getElementById('feedback-text').value = '';
    toast('Feedback submitted, replanning…', 'info');
    if (res.status === 'replan_started') {
      connectEventStream(currentProjectId);
      const btn = document.getElementById('run-btn');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner">↻</span> Replanning...';
    }
    loadProjects();
  } catch (e) {
    toast(e.message, 'error');
  }
}

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
  if (e.key === 'Enter' && e.target.id === 'new-brief' && (e.metaKey || e.ctrlKey)) {
    createProject();
  }
  if (e.key === 'Enter' && e.target.id === 'feedback-text' && (e.metaKey || e.ctrlKey)) {
    submitFeedback();
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
  if (name === 'plan') loadPlanView();
}

async function loadPlanView() {
  if (!currentProjectId) return;
  try {
    const proj = await api('GET', `/api/projects/${currentProjectId}`);
    renderPlan(proj.latest_plan_json);
  } catch (e) {}
}

function renderPlan(plan) {
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
    funny: 'bg-orange-900 text-orange-300' };

  const tones = (plan.style_tone || []).map(t =>
    `<span class="text-xs px-2 py-0.5 rounded-full ${toneColors[t] || 'bg-gray-800 text-gray-400'}">${t}</span>`
  ).join(' ');

  const bodyLines = (plan.script.body || []).map((l, i) =>
    `<p class="text-sm text-gray-300 py-1 border-b border-gray-800 last:border-0">${i + 1}. ${escHtml(l)}</p>`
  ).join('');

  const storyboard = (plan.storyboard || []).map((scene, i) => {
    const shot = (plan.shot_list || [])[i] || {};
    return `
    <div class="card p-3 fade-in">
      <div class="flex items-start gap-3">
        <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-800 flex items-center justify-center text-sm font-bold text-gray-400">${scene.scene}</div>
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-1.5 flex-wrap">
            <span class="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded font-mono">${assetIcon[scene.asset_hint] || '🎬'} ${scene.asset_hint || '?'}</span>
            <span class="text-xs text-gray-600">${scene.duration}s</span>
            ${shot.shot_id ? `<span class="text-xs text-gray-700 font-mono">${shot.shot_id}</span>` : ''}
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
        Storyboard · ${(plan.storyboard || []).length} scenes
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

// ── Init ───────────────────────────────────────────────────────────────────
loadProjects();
updateApiStatus();
// Refresh project list every 10s
setInterval(loadProjects, 10000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
