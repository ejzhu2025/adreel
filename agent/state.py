"""LangGraph state definition for video-agent-hero."""
from __future__ import annotations

from typing import Any, Optional
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    # ── Identity ──────────────────────────────────────────────────────────────
    project_id: str
    brief: str
    brand_id: str
    user_id: str

    # ── Loaded memory ─────────────────────────────────────────────────────────
    brand_kit: dict[str, Any]           # BrandKit as dict
    user_prefs: dict[str, Any]          # UserPrefs as dict
    similar_projects: list[dict]        # top-K retrieved from vector DB

    # ── Clarification ─────────────────────────────────────────────────────────
    clarification_needed: bool
    clarification_questions: list[dict]  # [{field, question, options}]
    clarification_answers: dict[str, Any]

    # ── Plan ──────────────────────────────────────────────────────────────────
    plan: dict[str, Any]                # Plan as dict
    plan_version: int
    plan_feedback: str                  # user feedback for replan
    creative_concept: dict[str, Any]    # Director's chosen concept (hook_angle, visual_style, …)
    t2v_prompts: dict[str, Any]         # shot_id -> {positive: str, negative: str} from PromptCompiler
    concept_images: dict[str, str]      # shot_id -> data URL (Gemini interleaved concept image)

    # ── Execution ─────────────────────────────────────────────────────────────
    scene_clips: list[dict]             # [{shot_id, clip_path, duration}]
    caption_segments: list[dict]        # CaptionSegment as dict list
    branded_clip_path: str              # after layout/branding

    # ── Quality ───────────────────────────────────────────────────────────────
    quality_result: dict[str, Any]      # QualityCheckResult as dict
    qc_attempt: int
    qc_diagnosis: str                   # root cause label
    qc_user_message: str                # human-readable explanation
    needs_user_action: bool             # stop pipeline and show message to user

    # ── Music ─────────────────────────────────────────────────────────────────
    music_track_path: str               # empty string = skipped

    # ── Output ────────────────────────────────────────────────────────────────
    output_path: str
    summary: str

    # ── Partial re-render ─────────────────────────────────────────────────────
    change_type: str                    # "global" | "local" | "add_scene" | "remove_scene"
    affected_shot_indices: list[int]    # 0-based shot indices to re-render (local)
    shot_updates: dict[str, Any]        # str(idx) -> {desc}
    new_shots: list[dict]               # add_scene: [{position, desc, type, duration}]
    remove_indices: list[int]           # remove_scene: 0-based indices to delete

    # ── Generation quality ────────────────────────────────────────────────────
    quality: str                        # "turbo" | "hd"

    # ── Assets ────────────────────────────────────────────────────────────────
    product_image_path: str             # absolute path to project-level product photo
    variant_image_paths: list[str]      # paths to color/variant product images (for color-cycle outro)

    # ── Control ───────────────────────────────────────────────────────────────
    needs_replan: bool
    error: Optional[str]
    messages: list[dict]                # conversation log
    relevance_rerender_attempt: int     # counts how many times relevance re-render has run


# ── Short-term memory (in-process) ────────────────────────────────────────────
# Shared mutable store — one instance per CLI session.

class WorkingMemory:
    """Lightweight in-memory store replacing Redis for MVP."""

    def __init__(self):
        self._store: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._store[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


# Global short-term memory singleton
working_memory = WorkingMemory()
