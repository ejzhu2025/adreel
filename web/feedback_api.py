"""web/feedback_api.py — /api/feedback/* endpoints + LLM reviewer background task."""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

import agent.deps as deps
from web.auth.deps import current_user, optional_user
from web.auth.models import User
from web.billing.credits import COSTS, add_credits

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])

DAILY_CREDIT_CAP = 500
MAX_SINGLE_AWARD_RATIO = 0.80


# ── Request model ──────────────────────────────────────────────────────────────

class FeedbackSubmit(BaseModel):
    project_id: str
    rating_overall: Optional[int] = None  # 1-5
    tags: list[str] = []
    text: str = ""


# ── Submit ─────────────────────────────────────────────────────────────────────

@router.post("")
async def submit_feedback(
    body: FeedbackSubmit,
    background_tasks: BackgroundTasks,
    user: Optional[User] = Depends(optional_user),
):
    """Submit feedback. Kicks off async LLM review to award credits."""
    db = deps.db()
    project = db.get_project(body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    user_id = user.id if user else ""
    user_name = user.name if user else ""

    # Estimate credits spent on this project
    plan = project.get("latest_plan_json") or {}
    shot_count = len(plan.get("shot_list", [])) or 5
    quality = plan.get("_quality", "turbo")
    credits_spent = shot_count * COSTS.get(f"shot_{quality}", 1)

    fid = db.add_feedback_v2(
        project_id=body.project_id,
        user_id=user_id,
        user_name=user_name,
        rating_overall=body.rating_overall,
        tags=body.tags,
        text=body.text,
        credits_spent=credits_spent,
    )
    background_tasks.add_task(_run_feedback_review, fid)
    return {"feedback_id": fid, "review_status": "pending"}


# ── Review status poll ─────────────────────────────────────────────────────────

@router.get("/{feedback_id}/review-status")
def review_status(feedback_id: int):
    db = deps.db()
    row = db.get_feedback_by_id(feedback_id)
    if not row:
        raise HTTPException(404, "Feedback not found")
    return {
        "review_status": row["review_status"],
        "credits_awarded": row["credits_awarded"],
        "review_reasoning": row.get("review_reasoning", "") if row["review_status"] == "reviewed" else "",
    }


# ── Dynamic categories ─────────────────────────────────────────────────────────

@router.get("/check/{project_id}")
def check_feedback(project_id: str, user: Optional[User] = Depends(optional_user)):
    """Return whether the current user has already submitted feedback for this project."""
    if not user:
        return {"has_feedback": False}
    db = deps.db()
    return {"has_feedback": db.has_feedback_for_project(user.id, project_id)}


@router.get("/categories")
def get_categories():
    """Return active top-5 categories (dynamically updated by daily analysis)."""
    db = deps.db()
    return db.get_active_feedback_categories()


# ── Per-user tracker ───────────────────────────────────────────────────────────

@router.get("/my")
def my_feedback(user: User = Depends(current_user)):
    """Return current user's feedback history with adoption status."""
    db = deps.db()
    rows = db.get_feedback_by_user(user.id)
    result = []
    for r in rows:
        tags = json.loads(r.get("tags") or "[]")
        # Derive human-readable status
        if r.get("fix_applied"):
            status = "adopted"
            status_label = "✅ Adopted"
        elif r.get("analysis_batch_id"):
            status = "reviewed"
            status_label = "🔍 In review"
        elif r.get("review_status") == "reviewed":
            status = "received"
            status_label = "✓ Received"
        else:
            status = "pending"
            status_label = "⏳ Pending"
        result.append({
            "id": r["id"],
            "project_id": r["project_id"],
            "brief": (r.get("brief") or "")[:80],
            "rating_overall": r.get("rating_overall"),
            "tags": tags,
            "text": (r.get("text") or "")[:200],
            "credits_awarded": r.get("credits_awarded", 0),
            "status": status,
            "status_label": status_label,
            "fix_notes": r.get("fix_notes", ""),
            "applied_at": r.get("applied_at", ""),
            "created_at": r.get("created_at", ""),
        })
    return result


# ── Changelog (for update banner) ─────────────────────────────────────────────

@router.get("/changelog")
def changelog():
    """Return recent adopted fixes with contributor attribution."""
    db = deps.db()
    fixes = db.get_adopted_fixes(limit=8)
    result = []
    for f in fixes:
        names = [n for n in (f.get("contributor_names") or "").split(",") if n.strip()]
        result.append({
            "date": (f.get("analysis_date") or f.get("applied_at") or "")[:10],
            "description": f.get("notes") or f.get("target_key", ""),
            "contributors": names[:3],
        })
    return result


# ── Admin: trigger analysis manually ──────────────────────────────────────────

@router.post("/admin/analyze")
async def trigger_analysis(background_tasks: BackgroundTasks):
    """Admin endpoint to manually trigger daily analysis."""
    from web.feedback_analysis import run_daily_analysis
    background_tasks.add_task(run_daily_analysis)
    return {"status": "analysis started"}


# ── LLM reviewer (background task) ────────────────────────────────────────────

def _run_feedback_review(feedback_id: int) -> None:
    """Score feedback with Claude Haiku. Award credits proportional to video cost."""
    import anthropic

    try:
        db = deps.db()
        row = db.get_feedback_by_id(feedback_id)
        if not row or row["review_status"] != "pending":
            return

        user_id = row.get("user_id", "")
        credits_spent = max(row.get("credits_spent") or 0, 1)

        # No credits for re-rating the same project
        project_id = row.get("project_id", "")
        if user_id and project_id:
            with __import__("sqlite3").connect(str(db.db_path)) as _c:
                already = _c.execute(
                    "SELECT id FROM feedback WHERE user_id=? AND project_id=? "
                    "AND review_status='reviewed' AND id!=?",
                    (user_id, project_id, feedback_id),
                ).fetchone()
            if already:
                db.update_feedback_review(feedback_id, 0, "Already rated this video — no extra credits.", 0)
                return

        # Anti-gaming: daily cap check
        if user_id:
            today_credits = db.get_daily_feedback_credits(user_id)
            if today_credits >= DAILY_CREDIT_CAP:
                db.update_feedback_review(feedback_id, 0, "Daily credit limit reached.", 0)
                return

        # Anti-gaming: duplicate within 10 min
        if user_id:
            recent = db.get_recent_feedback(user_id, row["project_id"], minutes=10)
            if len(recent) > 1:
                db.update_feedback_review(feedback_id, 0, "Duplicate submission.", 0)
                return

        text = (row.get("text") or "").strip()
        tags = json.loads(row.get("tags") or "[]")
        rating = row.get("rating_overall")

        # Fast path: absolutely nothing provided
        if not text and not tags and rating is None:
            db.update_feedback_review(feedback_id, 0, "No feedback content.", 0)
            return

        brief = (row.get("brief") or "")[:200]
        rating_str = f"{rating}/5" if rating is not None else "not rated"
        tags_str = ", ".join(tags) if tags else "none"

        prompt = f"""You are reviewing feedback about an AI-generated short video.

Project brief: {brief}
Overall rating: {rating_str}
Tags selected: {tags_str}
Free text: "{text[:500]}"

Score this feedback's usefulness for improving the AI video system (0-100).

Scoring guide:
- 80-100: Specific, actionable — mentions exact scene/element with context
- 60-79: Real issue identified, some detail
- 40-59: Genuine but generic (rating + 1 tag, or very short text)
- 20-39: Minimal signal (rating only, no text or tags)
- 0-19: Spam, gibberish, or contradictory (5 stars + negative complaint)

Hard caps:
- No text AND no tags → max 30
- Text under 15 chars → max 35
- Contradictory (rating 5 + clearly negative text) → max 25

Return ONLY valid JSON, no markdown:
{{"score": <0-100>, "reasoning": "<one friendly sentence for the user>", "is_spam": <true|false>}}"""

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = resp.content[0].text.strip()
        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        result = json.loads(raw_text.strip())
        score = max(0, min(100, int(result.get("score", 0))))
        reasoning = result.get("reasoning", "Thanks for your feedback!")

        # Map score → percentage of credits_spent to award
        if score >= 80:
            pct = 0.80
        elif score >= 60:
            pct = 0.55
        elif score >= 40:
            pct = 0.30
        elif score >= 20:
            pct = 0.10
        else:
            pct = 0.0

        raw_award = int(credits_spent * pct)
        max_single = min(int(credits_spent * MAX_SINGLE_AWARD_RATIO), 200)
        credits = min(raw_award, max_single)

        # Respect daily cap
        if user_id:
            today_credits = db.get_daily_feedback_credits(user_id)
            credits = max(0, min(credits, DAILY_CREDIT_CAP - today_credits))

        db.update_feedback_review(feedback_id, score, reasoning, credits)

        if credits > 0 and user_id:
            add_credits(user_id, credits)
            logger.info(
                "Feedback %d reviewed: score=%d credits=%d user=%s",
                feedback_id, score, credits, user_id,
            )

    except Exception as exc:
        logger.error("Feedback review failed id=%d: %s", feedback_id, exc)
        try:
            deps.db().update_feedback_review(
                feedback_id, 0, "Review unavailable — no credits awarded.", 0
            )
        except Exception:
            pass
