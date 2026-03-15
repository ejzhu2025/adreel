"""web/routers/projects.py — Project CRUD + run/plan/execute/modify/events endpoints."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import agent.deps as deps
from web.auth.deps import optional_user
from web.app_state import _run_events, _run_queues

# Comma-separated list of valid guest access codes, e.g. VAH_GUEST_CODES=DEMO2024,BETA123
_GUEST_CODES: set[str] = {
    c.strip() for c in os.environ.get("VAH_GUEST_CODES", "").split(",") if c.strip()
}

GUEST_COOKIE = "vah_guest"


def _guest_code_valid(request: Request) -> bool:
    """Return True if the request carries a valid guest access code cookie."""
    code = request.cookies.get(GUEST_COOKIE, "")
    return bool(_GUEST_CODES) and code in _GUEST_CODES


# user_ids that existed before auth was added — treated as legacy (not shown to anyone)
_LEGACY_USER_IDS: frozenset[str] = frozenset({"ej", "default", ""})


def _resolve_user_id(auth_user, request: Request) -> str:
    """Return a stable string user_id for the current requester, or raise 401."""
    if auth_user:
        return auth_user.id
    if _guest_code_valid(request):
        return "guest"
    raise HTTPException(status_code=401, detail="Login required or enter an access code")


def _check_project_ownership(proj: dict, user_id: str) -> None:
    """Raise 403 if the requester doesn't own this project."""
    if proj.get("user_id") == user_id:
        return
    raise HTTPException(status_code=403, detail="Access denied")


def _billing_user_id(auth_user, request: Request) -> str | None:
    """Return the user_id to use for billing, or raise 401 if access is denied.
    - Logged-in user  → their user_id (credits deducted normally)
    - Guest with valid code cookie → None (free, no deduction)
    - Anyone else → 401
    """
    if auth_user:
        return auth_user.id
    if _guest_code_valid(request):
        return None  # valid guest → unlimited
    raise HTTPException(status_code=401, detail="Login required or enter an access code")

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request models ────────────────────────────────────────────────────────────


class CreateProjectRequest(BaseModel):
    brief: str
    brand_id: str = "tong_sui"


class RunRequest(BaseModel):
    skip_clarification: bool = True


class FeedbackRequest(BaseModel):
    text: str
    rating: int | None = None
    replan: bool = True


class PlanRequest(BaseModel):
    brief: str = ""
    brand_id: str = "tong_sui"
    clarification_answers: dict = {}
    plan_feedback: str = ""  # non-empty → replan from existing plan


class ExecuteRequest(BaseModel):
    plan: dict | None = None  # None → use DB-stored plan
    clarification_answers: dict = {}
    quality: str = "turbo"  # "turbo" | "hd"


class ModifyRequest(BaseModel):
    text: str
    quality: str = ""  # empty → inherit from current plan's _quality


class RerenderShotRequest(BaseModel):
    shot_index: int
    quality: str = "turbo"


# ── Helper utilities ──────────────────────────────────────────────────────────


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


def _get_project_product_image_path(project_id: str) -> str:
    data_dir = Path(os.environ.get("VAH_DATA_DIR", "./data"))
    p = data_dir / "projects" / project_id / "product.png"
    return str(p) if p.exists() else ""


def _generate_project_title(project_id: str, brief: str, plan: dict) -> None:
    """Call Claude Haiku to generate a short project title and store it."""
    import anthropic
    try:
        shot_titles = [s.get("title", "") for s in (plan.get("storyboard") or [])[:3] if s.get("title")]
        context = f"Brief: {brief[:200]}"
        if shot_titles:
            context += f"\nFirst scenes: {', '.join(shot_titles)}"
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            messages=[{"role": "user", "content":
                f"{context}\n\nGive this video project a concise title in 2-4 words. "
                f"No quotes, no punctuation, just the title."}],
        )
        title = resp.content[0].text.strip().strip('"\'').strip()
        if title:
            deps.db().set_project_title(project_id, title)
    except Exception as exc:
        logger.warning("Title generation failed for %s: %s", project_id, exc)


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


# Expected wait time for slow nodes (shown in the UI spinner)
_NODE_ESTIMATED_WAIT: dict[str, str] = {
    "planner_llm":       "usually takes 3-4 minutes",
    "executor_pipeline": "usually takes 3-5 minutes",
    "partial_executor":  "usually takes 1-2 minutes",
}


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
                        if "triggers" in chunk:
                            node_name = chunk.get("name", "")
                            ts = datetime.now(timezone.utc).isoformat()
                            node_started_at[node_name] = ts
                            evt: dict = {
                                "type": "node_start",
                                "node": node_name,
                                "timestamp": ts,
                            }
                            wait_hint = _NODE_ESTIMATED_WAIT.get(node_name)
                            if wait_hint:
                                evt["estimated_wait"] = wait_hint
                            _emit(evt)
                    elif mode == "updates":
                        for node_name, node_output in chunk.items():
                            ts = datetime.now(timezone.utc).isoformat()
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

            _emit({"type": "done", "timestamp": datetime.now(timezone.utc).isoformat()})
            deps.db().update_project_status(project_id, "done")

        except Exception as exc:
            import traceback
            _emit({
                "type": "error",
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            deps.db().update_project_status(project_id, "failed")

    await asyncio.to_thread(run_in_thread)


# ── Project CRUD ──────────────────────────────────────────────────────────────


@router.get("/api/projects")
async def list_projects(request: Request, auth_user=Depends(optional_user)):
    user_id = _resolve_user_id(auth_user, request)
    return deps.db().list_projects(user_id=user_id, limit=30)


@router.post("/api/projects")
async def create_project(req: CreateProjectRequest, request: Request, auth_user=Depends(optional_user)):
    user_id = _resolve_user_id(auth_user, request)
    pid = deps.db().create_project(
        brief=req.brief, brand_id=req.brand_id, user_id=user_id,
    )
    return {"project_id": pid}


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request, auth_user=Depends(optional_user)):
    user_id = _resolve_user_id(auth_user, request)
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_ownership(proj, user_id)
    data_dir = Path(os.environ.get("VAH_DATA_DIR", "./data"))
    product_path = data_dir / "projects" / project_id / "product.png"
    proj["has_product_image"] = product_path.exists()
    return proj


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request, auth_user=Depends(optional_user)):
    user_id = _resolve_user_id(auth_user, request)
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_ownership(proj, user_id)
    deps.db().delete_project(project_id)
    return {"status": "deleted"}


# ── Product image ─────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/product-image")
async def upload_project_product_image(
    project_id: str, file: UploadFile = File(...),
    request: Request = None, auth_user=Depends(optional_user),
):
    user_id = _resolve_user_id(auth_user, request)
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_ownership(proj, user_id)
    content = await file.read()
    if len(content) < 10:
        raise HTTPException(status_code=400, detail="File too small")
    data_dir = Path(os.environ.get("VAH_DATA_DIR", "./data"))
    proj_dir = data_dir / "projects" / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    product_path = proj_dir / "product.png"
    product_path.write_bytes(content)
    return {"status": "ok", "path": str(product_path)}


@router.get("/api/projects/{project_id}/product-image")
async def get_project_product_image(project_id: str):
    path = _get_project_product_image_path(project_id)
    if not path:
        raise HTTPException(status_code=404, detail="No product image for this project")
    return FileResponse(path)


# ── Run (legacy full pipeline) ────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/run")
async def run_project(
    project_id: str, req: RunRequest, background_tasks: BackgroundTasks
):
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

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


# ── Plan ──────────────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/plan")
async def plan_project(
    project_id: str, req: PlanRequest, background_tasks: BackgroundTasks,
    request: Request, auth_user=Depends(optional_user),
):
    """Run planning-only phase; stores plan in DB and sets status='planned'."""
    user_id = _resolve_user_id(auth_user, request)
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_ownership(proj, user_id)

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
                _generate_project_title(project_id, proj["brief"], plan)

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


# ── Execute ───────────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/execute")
async def execute_project(
    project_id: str, req: ExecuteRequest, background_tasks: BackgroundTasks,
    request: Request, auth_user=Depends(optional_user),
):
    """Run execution-only phase using stored or provided plan."""
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_ownership(proj, _resolve_user_id(auth_user, request))

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

    deps.db().update_project_plan(project_id, {**plan, "_quality": quality})

    user_id = _billing_user_id(auth_user, request)
    shot_count = len(plan.get("shot_list") or []) or 7
    from web.billing.credits import COSTS, get_credits
    cost_per_shot = COSTS["shot_hd"] if quality == "hd" else COSTS["shot_turbo"]
    estimated_cost = shot_count * cost_per_shot
    if user_id:
        balance = get_credits(user_id)
        if balance < estimated_cost:
            raise HTTPException(status_code=402, detail={
                "message": f"Insufficient credits: need {estimated_cost}, have {balance}",
                "needed": estimated_cost,
                "have": balance,
            })

    def _on_execute_node(node_name: str, node_output: dict):
        if node_name == "result_summarizer":
            final_plan = node_output.get("plan") or plan
            actual_shots = len(final_plan.get("shot_list") or []) or shot_count
            actual_cost = actual_shots * cost_per_shot
            if user_id:
                try:
                    from web.billing.credits import deduct_credits
                    deduct_credits(user_id, actual_cost)
                    logger.info("Deducted %d credits from %s (execute %s, %s quality)",
                                actual_cost, user_id, project_id, quality)
                except ValueError as e:
                    logger.warning("Credit deduction failed: %s", e)
            current = deps.db().get_project(project_id)
            if not current or not current.get("title"):
                _generate_project_title(project_id, proj["brief"], final_plan)

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
    return {"status": "execute_started", "quality": quality, "estimated_cost": estimated_cost}


# ── Rerender single shot ──────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/rerender-shot")
async def rerender_shot(
    project_id: str, req: RerenderShotRequest, background_tasks: BackgroundTasks,
    request: Request, auth_user=Depends(optional_user),
):
    """Re-render a single shot by index without replanning or change classification."""
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_ownership(proj, _resolve_user_id(auth_user, request))

    plan = proj.get("latest_plan_json") or {}
    if not plan:
        raise HTTPException(status_code=400, detail="No plan found; generate a video first")

    shot_list = plan.get("shot_list", [])
    if req.shot_index < 0 or req.shot_index >= len(shot_list):
        raise HTTPException(status_code=400, detail=f"Invalid shot_index {req.shot_index}")

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
    from web.billing.credits import COSTS, get_credits
    cost_per_shot = COSTS["shot_hd"] if quality == "hd" else COSTS["shot_turbo"]
    user_id = _billing_user_id(auth_user, request)
    if user_id:
        balance = get_credits(user_id)
        if balance < cost_per_shot:
            raise HTTPException(status_code=402, detail={
                "message": f"Insufficient credits: need {cost_per_shot}, have {balance}",
                "needed": cost_per_shot,
                "have": balance,
            })

    initial_state: dict = {
        "project_id": project_id,
        "brief": proj["brief"],
        "brand_id": proj["brand_id"],
        "user_id": proj["user_id"],
        "messages": [],
        "clarification_answers": answers,
        "plan": plan,
        "plan_version": plan.get("version", 1),
        "plan_feedback": f"Re-render shot {req.shot_index + 1}",
        "qc_attempt": 1,
        "needs_replan": False,
        "brand_kit": brand_kit_obj.model_dump() if brand_kit_obj else {},
        "user_prefs": {},
        "similar_projects": [],
        "quality": quality,
        "affected_shot_indices": [req.shot_index],
        "shot_updates": {},
    }

    def _on_rerender_node(node_name: str, node_output: dict):
        if node_name in ("partial_executor", "result_summarizer"):
            new_plan = node_output.get("plan")
            if new_plan:
                deps.db().update_project_plan(project_id, {**new_plan, "_quality": quality})
        if node_name == "partial_executor":
            if user_id and cost_per_shot > 0:
                try:
                    from web.billing.credits import deduct_credits
                    deduct_credits(user_id, cost_per_shot)
                    logger.info("Deducted %d credits from %s (rerender-shot %s idx=%d)",
                                cost_per_shot, user_id, project_id, req.shot_index)
                except ValueError as e:
                    logger.warning("Credit deduction failed for rerender-shot: %s", e)

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
        on_node=_on_rerender_node,
    )
    return {"status": "rerender_started", "shot_index": req.shot_index,
            "quality": quality, "estimated_cost": cost_per_shot}


# ── Modify ────────────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/modify")
async def modify_project(
    project_id: str, req: ModifyRequest, background_tasks: BackgroundTasks,
    request: Request, auth_user=Depends(optional_user),
):
    """Smart modify: classify feedback → local (partial re-render) or global (full replan)."""
    proj = deps.db().get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_ownership(proj, _resolve_user_id(auth_user, request))

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

    modify_user_id = _billing_user_id(auth_user, request)
    from web.billing.credits import COSTS as _COSTS
    _cost_per_shot = _COSTS["shot_hd"] if quality == "hd" else _COSTS["shot_turbo"]

    def _on_modify_node(node_name: str, node_output: dict):
        if node_name in ("partial_executor", "result_summarizer"):
            new_plan = node_output.get("plan")
            if new_plan:
                deps.db().update_project_plan(project_id, {**new_plan, "_quality": quality})
        if node_name == "partial_executor":
            rerendered = node_output.get("rerendered_shot_indices") or []
            n = len(rerendered) if rerendered else len((node_output.get("plan") or plan).get("shot_list") or [])
            cost = n * _cost_per_shot
            if modify_user_id and cost > 0:
                try:
                    from web.billing.credits import deduct_credits
                    deduct_credits(modify_user_id, cost)
                    logger.info("Deducted %d credits from %s (modify %s, %d shots)",
                                cost, modify_user_id, project_id, n)
                except ValueError as e:
                    logger.warning("Credit deduction failed for modify: %s", e)
        elif node_name == "result_summarizer" and not node_output.get("_partial"):
            final_plan = node_output.get("plan") or plan
            n = len(final_plan.get("shot_list") or [])
            cost = n * _cost_per_shot
            if modify_user_id and cost > 0:
                try:
                    from web.billing.credits import deduct_credits
                    deduct_credits(modify_user_id, cost)
                    logger.info("Deducted %d credits from %s (modify+replan %s, %d shots)",
                                cost, modify_user_id, project_id, n)
                except ValueError as e:
                    logger.warning("Credit deduction failed for modify+replan: %s", e)
            if node_name == "result_summarizer":
                current = deps.db().get_project(project_id)
                if not current or not current.get("title"):
                    _generate_project_title(project_id, proj["brief"], node_output.get("plan") or plan)

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


# ── Feedback + replan ─────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/feedback")
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


# ── SSE events stream ─────────────────────────────────────────────────────────


@router.get("/api/projects/{project_id}/events")
async def stream_events(project_id: str):
    """SSE stream of agent execution events for a project."""

    async def generate():
        yield "retry: 3000\n\n"

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
