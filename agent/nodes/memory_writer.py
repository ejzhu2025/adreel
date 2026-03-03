"""memory_writer — persist plan, output path, and summary embedding to long-term memory."""
from __future__ import annotations

from typing import Any

import agent.deps as deps


def memory_writer(state: dict[str, Any]) -> dict[str, Any]:
    db = deps.db()
    vs = deps.vs()

    project_id = state.get("project_id", "unknown")
    plan = state.get("plan", {})
    output_path = state.get("output_path", "")
    summary = state.get("summary", "")
    brand_id = state.get("brand_id", "default")
    plan_feedback = state.get("plan_feedback", "")

    # Persist plan to SQLite
    if plan:
        db.update_project_plan(project_id, plan)

    # Persist output path
    if output_path:
        db.update_project_output(project_id, output_path, status="done")

    # Store feedback if present
    if plan_feedback:
        db.add_feedback(project_id, plan_feedback)

    # Write embedding to vector store
    if summary:
        metadata = {
            "project_id": project_id,
            "brand_id": brand_id,
            "platform": plan.get("platform", ""),
            "duration_sec": str(plan.get("duration_sec", "")),
            "language": plan.get("language", ""),
            "tone": ", ".join(plan.get("style_tone", [])),
        }
        vs.add(doc_id=project_id, text=summary, metadata=metadata)

    messages = state.get("messages", [])
    messages.append(
        {
            "role": "system",
            "content": f"[memory_writer] saved project={project_id} to DB + vector store",
        }
    )

    return {"messages": messages}
