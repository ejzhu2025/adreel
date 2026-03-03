"""LangGraph state machine — wires all nodes into an executable workflow."""
from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph, END

from agent.state import AgentState
from agent.nodes.change_classifier import change_classifier
from agent.nodes.partial_executor import partial_executor
from agent.nodes.intent_parser import intent_parser
from agent.nodes.memory_loader import memory_loader
from agent.nodes.clarification_planner import clarification_planner
from agent.nodes.ask_user import ask_user
from agent.nodes.planner_llm import planner_llm
from agent.nodes.plan_checker import plan_checker
from agent.nodes.executor_pipeline import executor_pipeline
from agent.nodes.caption_agent import caption_agent
from agent.nodes.layout_branding import layout_branding
from agent.nodes.quality_gate import quality_gate
from agent.nodes.qc_diagnose import qc_diagnose
from agent.nodes.render_export import render_export
from agent.nodes.result_summarizer import result_summarizer
from agent.nodes.memory_writer import memory_writer


# ── Routing functions ─────────────────────────────────────────────────────────

def _route_clarification(state: dict[str, Any]) -> str:
    return "ask_user" if state.get("clarification_needed") else "planner_llm"


def _route_plan_checker(state: dict[str, Any]) -> str:
    """If plan needs replan and we haven't tried more than 3 times, loop back."""
    if state.get("needs_replan") and state.get("plan_version", 1) < 3:
        return "planner_llm"
    return "executor_pipeline"


def _route_quality_gate(state: dict[str, Any]) -> str:
    qr = state.get("quality_result", {})
    if not qr.get("passed"):
        return "qc_diagnose"
    return "render_export"


def _route_qc_diagnose(state: dict[str, Any]) -> str:
    if state.get("needs_user_action"):
        return END
    action = state.get("qc_diagnosis", "")
    attempt = state.get("qc_attempt", 1)
    if action in ("wrong_resolution", "duration_mismatch", "fal_bad_output") and attempt <= 2:
        return "layout_branding"
    return "render_export"


# ── Main full pipeline ────────────────────────────────────────────────────────

def build_graph() -> Any:
    """Build the full video generation pipeline graph."""
    workflow = StateGraph(AgentState)

    # Register nodes
    workflow.add_node("intent_parser", intent_parser)
    workflow.add_node("memory_loader", memory_loader)
    workflow.add_node("clarification_planner", clarification_planner)
    workflow.add_node("ask_user", ask_user)
    workflow.add_node("planner_llm", planner_llm)
    workflow.add_node("plan_checker", plan_checker)
    workflow.add_node("executor_pipeline", executor_pipeline)
    workflow.add_node("caption_agent", caption_agent)
    workflow.add_node("layout_branding", layout_branding)
    workflow.add_node("quality_gate", quality_gate)
    workflow.add_node("qc_diagnose", qc_diagnose)
    workflow.add_node("render_export", render_export)
    workflow.add_node("result_summarizer", result_summarizer)
    workflow.add_node("memory_writer", memory_writer)

    # Entry point
    workflow.set_entry_point("intent_parser")

    # Linear edges
    workflow.add_edge("intent_parser", "memory_loader")
    workflow.add_edge("memory_loader", "clarification_planner")
    workflow.add_edge("ask_user", "planner_llm")
    workflow.add_edge("executor_pipeline", "caption_agent")
    workflow.add_edge("caption_agent", "layout_branding")
    workflow.add_edge("layout_branding", "quality_gate")
    workflow.add_edge("render_export", "result_summarizer")
    workflow.add_edge("result_summarizer", "memory_writer")
    workflow.add_edge("memory_writer", END)

    # Conditional edges
    workflow.add_conditional_edges(
        "clarification_planner",
        _route_clarification,
        {"ask_user": "ask_user", "planner_llm": "planner_llm"},
    )
    workflow.add_edge("planner_llm", "plan_checker")

    # Replan loop or proceed to execution
    workflow.add_conditional_edges(
        "plan_checker",
        _route_plan_checker,
        {"planner_llm": "planner_llm", "executor_pipeline": "executor_pipeline"},
    )
    workflow.add_conditional_edges(
        "quality_gate",
        _route_quality_gate,
        {"qc_diagnose": "qc_diagnose", "render_export": "render_export"},
    )
    workflow.add_conditional_edges(
        "qc_diagnose",
        _route_qc_diagnose,
        {END: END, "layout_branding": "layout_branding", "render_export": "render_export"},
    )

    return workflow.compile()


# ── Plan-only graph (used by /api/projects/{id}/plan) ─────────────────────────

def _route_plan_checker_plan_only(state: dict[str, Any]) -> str:
    if state.get("needs_replan") and state.get("plan_version", 1) < 3:
        return "planner_llm"
    return END


def build_plan_only_graph() -> Any:
    """Runs planning steps only: intent_parser → … → plan_checker → END."""
    workflow = StateGraph(AgentState)

    workflow.add_node("intent_parser", intent_parser)
    workflow.add_node("memory_loader", memory_loader)
    workflow.add_node("clarification_planner", clarification_planner)
    workflow.add_node("ask_user", ask_user)
    workflow.add_node("planner_llm", planner_llm)
    workflow.add_node("plan_checker", plan_checker)

    workflow.set_entry_point("intent_parser")

    workflow.add_edge("intent_parser", "memory_loader")
    workflow.add_edge("memory_loader", "clarification_planner")
    workflow.add_edge("ask_user", "planner_llm")
    workflow.add_edge("planner_llm", "plan_checker")

    workflow.add_conditional_edges(
        "clarification_planner",
        _route_clarification,
        {"ask_user": "ask_user", "planner_llm": "planner_llm"},
    )
    workflow.add_conditional_edges(
        "plan_checker",
        _route_plan_checker_plan_only,
        {"planner_llm": "planner_llm", END: END},
    )

    return workflow.compile()


# ── Execute-only graph (used by /api/projects/{id}/execute) ───────────────────

def build_execute_only_graph() -> Any:
    """Runs execution steps only: executor_pipeline → … → memory_writer → END."""
    workflow = StateGraph(AgentState)

    workflow.add_node("executor_pipeline", executor_pipeline)
    workflow.add_node("caption_agent", caption_agent)
    workflow.add_node("layout_branding", layout_branding)
    workflow.add_node("quality_gate", quality_gate)
    workflow.add_node("qc_diagnose", qc_diagnose)
    workflow.add_node("render_export", render_export)
    workflow.add_node("result_summarizer", result_summarizer)
    workflow.add_node("memory_writer", memory_writer)

    workflow.set_entry_point("executor_pipeline")

    workflow.add_edge("executor_pipeline", "caption_agent")
    workflow.add_edge("caption_agent", "layout_branding")
    workflow.add_edge("layout_branding", "quality_gate")
    workflow.add_edge("render_export", "result_summarizer")
    workflow.add_edge("result_summarizer", "memory_writer")
    workflow.add_edge("memory_writer", END)

    workflow.add_conditional_edges(
        "quality_gate",
        _route_quality_gate,
        {"qc_diagnose": "qc_diagnose", "render_export": "render_export"},
    )
    workflow.add_conditional_edges(
        "qc_diagnose",
        _route_qc_diagnose,
        {END: END, "layout_branding": "layout_branding", "render_export": "render_export"},
    )

    return workflow.compile()


# ── Partial re-render graph (used by /modify endpoint) ───────────────────────

def _route_change_classifier(state: dict[str, Any]) -> str:
    if state.get("change_type") == "local":
        return "partial_executor"
    return "planner_llm"


def build_partial_rerender_graph() -> Any:
    """Smart modify: change_classifier → local→partial_executor or global→planner_llm."""
    workflow = StateGraph(AgentState)

    workflow.add_node("change_classifier", change_classifier)
    workflow.add_node("partial_executor", partial_executor)
    workflow.add_node("planner_llm", planner_llm)
    workflow.add_node("plan_checker", plan_checker)
    workflow.add_node("executor_pipeline", executor_pipeline)
    workflow.add_node("caption_agent", caption_agent)
    workflow.add_node("layout_branding", layout_branding)
    workflow.add_node("quality_gate", quality_gate)
    workflow.add_node("qc_diagnose", qc_diagnose)
    workflow.add_node("render_export", render_export)
    workflow.add_node("result_summarizer", result_summarizer)
    workflow.add_node("memory_writer", memory_writer)

    workflow.set_entry_point("change_classifier")

    # Classifier branches
    workflow.add_conditional_edges(
        "change_classifier",
        _route_change_classifier,
        {"partial_executor": "partial_executor", "planner_llm": "planner_llm"},
    )

    # Global path: full replan then execute
    workflow.add_edge("planner_llm", "plan_checker")
    workflow.add_conditional_edges(
        "plan_checker",
        _route_plan_checker,
        {"planner_llm": "planner_llm", "executor_pipeline": "executor_pipeline"},
    )
    workflow.add_edge("executor_pipeline", "caption_agent")

    # Local path: partial re-render
    workflow.add_edge("partial_executor", "caption_agent")

    # Shared tail
    workflow.add_edge("caption_agent", "layout_branding")
    workflow.add_edge("layout_branding", "quality_gate")
    workflow.add_conditional_edges(
        "quality_gate",
        _route_quality_gate,
        {"qc_diagnose": "qc_diagnose", "render_export": "render_export"},
    )
    workflow.add_conditional_edges(
        "qc_diagnose",
        _route_qc_diagnose,
        {END: END, "layout_branding": "layout_branding", "render_export": "render_export"},
    )
    workflow.add_edge("render_export", "result_summarizer")
    workflow.add_edge("result_summarizer", "memory_writer")
    workflow.add_edge("memory_writer", END)

    return workflow.compile()


# ── Replan-only graph (used by feedback command) ──────────────────────────────

def build_replan_graph() -> Any:
    """Minimal graph: planner_llm → plan_checker → executor → … → END."""
    workflow = StateGraph(AgentState)

    workflow.add_node("planner_llm", planner_llm)
    workflow.add_node("plan_checker", plan_checker)
    workflow.add_node("executor_pipeline", executor_pipeline)
    workflow.add_node("caption_agent", caption_agent)
    workflow.add_node("layout_branding", layout_branding)
    workflow.add_node("quality_gate", quality_gate)
    workflow.add_node("qc_diagnose", qc_diagnose)
    workflow.add_node("render_export", render_export)
    workflow.add_node("result_summarizer", result_summarizer)
    workflow.add_node("memory_writer", memory_writer)

    workflow.set_entry_point("planner_llm")

    workflow.add_edge("planner_llm", "plan_checker")
    workflow.add_conditional_edges(
        "plan_checker",
        _route_plan_checker,
        {"planner_llm": "planner_llm", "executor_pipeline": "executor_pipeline"},
    )
    workflow.add_edge("executor_pipeline", "caption_agent")
    workflow.add_edge("caption_agent", "layout_branding")
    workflow.add_edge("layout_branding", "quality_gate")
    workflow.add_conditional_edges(
        "quality_gate",
        _route_quality_gate,
        {"qc_diagnose": "qc_diagnose", "render_export": "render_export"},
    )
    workflow.add_conditional_edges(
        "qc_diagnose",
        _route_qc_diagnose,
        {END: END, "layout_branding": "layout_branding", "render_export": "render_export"},
    )
    workflow.add_edge("render_export", "result_summarizer")
    workflow.add_edge("result_summarizer", "memory_writer")
    workflow.add_edge("memory_writer", END)

    return workflow.compile()
