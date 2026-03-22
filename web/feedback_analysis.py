"""web/feedback_analysis.py — Daily feedback analysis pipeline.

Can be run:
  - Automatically at midnight UTC (registered at FastAPI startup)
  - Manually via POST /api/feedback/admin/analyze
  - From command line: python3.11 -m web.feedback_analysis
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from web.token_tracker import log_tokens


def _strip_fences(text: str) -> str:
    """Strip markdown code fences from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run_daily_analysis(date_str: str | None = None) -> dict:
    """Run daily feedback analysis. Returns the analysis report dict."""
    # Ensure project root on path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import agent.deps as deps

    db = deps.db()
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    feedback_rows = db.get_feedback_for_analysis(since=since)
    logger.info("[analysis] %s: %d feedback items to analyze", date_str, len(feedback_rows))

    if not feedback_rows:
        return {"feedback_count": 0, "message": "No feedback to analyze"}

    # 1. Mine categories from free text → update feedback_categories table
    _mine_categories(feedback_rows, db)

    # 2. Build structured pain-point report
    report = _build_analysis_report(feedback_rows, date_str, db)

    # 3. Generate config fixes
    fixes = _generate_fixes(report, db)

    # 4. Persist
    db.save_analysis(
        batch_id=date_str,
        feedback_count=len(feedback_rows),
        report=report,
        fixes=fixes,
    )

    # 5. Mark feedback as analyzed
    db.mark_feedback_analyzed([r["id"] for r in feedback_rows], date_str)

    # 6. Apply high-confidence safe fixes
    _apply_fixes(fixes, batch_id=date_str, db=db)

    logger.info("[analysis] %s complete. Pain points: %d. Fixes generated: %d",
                date_str, len(report.get("top_pain_points", [])), len(fixes))
    return report


# ── Category mining ────────────────────────────────────────────────────────────

def _mine_categories(feedback_rows: list[dict], db) -> None:
    """Extract top recurring themes from free text and update feedback_categories."""
    import anthropic

    texts = [r["text"] for r in feedback_rows if (r.get("text") or "").strip()]
    if len(texts) < 2:
        return

    existing = db.get_all_feedback_categories()
    existing_labels = [c["label"] for c in existing]

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    combined = "\n".join(f"- {t[:200]}" for t in texts[:80])
    existing_str = json.dumps(existing_labels) if existing_labels else "[]"

    prompt = f"""Analyze these user feedback comments about an AI short-video generator.
Extract the top 5 most recurring themes as short category labels (2-4 words).

Existing categories (reuse exact label if theme is the same): {existing_str}

Feedback:
{combined}

Return ONLY a JSON array, no markdown:
[{{"label": "<2-4 word label>", "description": "<one sentence>"}}]

Rules:
- If a theme matches an existing label closely, use that exact existing label
- Labels should be lowercase, concise (e.g. "shots off-brief", "music mismatch")
- Max 5 items, ordered by apparent frequency"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        log_tokens("claude-haiku-4-5-20251001", "category_mining", resp.usage)
        cats = json.loads(_strip_fences(resp.content[0].text))
        for cat in cats:
            db.upsert_feedback_category(
                label=cat.get("label", "").strip().lower(),
                description=cat.get("description", ""),
            )
        logger.info("[analysis] Mined %d categories", len(cats))
    except Exception as exc:
        logger.error("[analysis] Category mining failed: %s", exc)


# ── Pain-point report ──────────────────────────────────────────────────────────

def _build_analysis_report(feedback_rows: list[dict], date_str: str, db) -> dict:
    """Use Claude Sonnet to produce structured pain-point analysis."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Previous summaries for trend context
    prev_analyses = db.get_recent_analyses(limit=3)
    prev_summaries = "\n".join(
        f"- {a['analysis_date']}: {a['report_json'].get('executive_summary', '')}"
        for a in prev_analyses
        if a.get("report_json")
    ) or "No previous data"

    # Format feedback compactly
    formatted = []
    for r in feedback_rows[:150]:
        parts = [f"Brief: {(r.get('brief') or '')[:80]}"]
        if r.get("rating_overall"):
            parts.append(f"Rating: {r['rating_overall']}/5")
        tags = json.loads(r.get("tags") or "[]")
        if tags:
            parts.append(f"Tags: {', '.join(tags)}")
        if r.get("text"):
            parts.append(f"Text: {(r['text'])[:200]}")
        formatted.append(" | ".join(parts))

    prompt = f"""You are a product analyst for an AI video generation SaaS.
Analyze {len(feedback_rows)} user feedback items from {date_str}.

Previous 3 days summaries (for trend):
{prev_summaries}

--- FEEDBACK ---
{chr(10).join(formatted)}
--- END ---

Return ONLY valid JSON, no markdown:
{{
  "executive_summary": "<2-3 sentence summary of today's feedback>",
  "top_pain_points": [
    {{
      "title": "<short title>",
      "frequency": <count of items mentioning this>,
      "severity": <1-10>,
      "example_quote": "<real quote from feedback above>",
      "root_cause": "<hypothesis about why this happens>",
      "recommended_fix": "<specific actionable suggestion>",
      "config_key": "<system_config key if auto-fixable, else null>"
    }}
  ],
  "positive_signals": ["<thing users liked>"],
  "trend": "improving|stable|declining|insufficient_data",
  "priority_action": "<single most important fix today>"
}}

Include up to 5 pain points. Only set config_key if the fix maps directly to:
relevance_threshold, planner_prompt_addendum, director_style_addendum,
caption_font_size_default, t2v_negative_prompt_addendum, music_volume_db, shot_count_default"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        log_tokens("claude-sonnet-4-6", "feedback_analysis", resp.usage)
        return json.loads(_strip_fences(resp.content[0].text))
    except Exception as exc:
        logger.error("[analysis] Report build failed: %s", exc)
        return {
            "executive_summary": "Analysis failed.",
            "top_pain_points": [],
            "positive_signals": [],
            "trend": "insufficient_data",
            "priority_action": "",
        }


# ── Fix generation ─────────────────────────────────────────────────────────────

def _generate_fixes(report: dict, db) -> list[dict]:
    """Ask Claude Haiku to generate concrete system_config patches."""
    import anthropic

    pain_points = [p for p in report.get("top_pain_points", []) if p.get("config_key")]
    if not pain_points:
        return []

    current_configs = db.list_system_configs()
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = f"""Based on this feedback analysis, generate specific config changes.

Pain points with config keys:
{json.dumps(pain_points, indent=2)}

Current config values:
{json.dumps(current_configs, indent=2)}

Available config keys and their types/defaults:
- relevance_threshold (int, default 5): quality gate score threshold 1-10
- planner_prompt_addendum (str, default ""): extra rules appended to planner
- director_style_addendum (str, default ""): extra style instructions for director
- caption_font_size_default (int, default 44): subtitle font size in pixels
- t2v_negative_prompt_addendum (str, default ""): added to T2V negative prompts
- music_volume_db (float, default 0): music volume offset in dB (-6 to +6)
- shot_count_default (int, default 7): default number of shots

Return ONLY valid JSON array, no markdown. Max 3 fixes. Only include confidence >= 0.65:
[{{
  "target_key": "<key>",
  "old_value": <current value or null>,
  "new_value": <proposed value>,
  "rationale": "<why this helps, shown to admin>",
  "confidence": <0.0-1.0>,
  "estimated_impact": "<what metric should improve>"
}}]"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        log_tokens("claude-haiku-4-5-20251001", "fix_generation", resp.usage)
        fixes = json.loads(_strip_fences(resp.content[0].text))
        return [f for f in fixes if isinstance(f, dict) and f.get("confidence", 0) >= 0.65]
    except Exception as exc:
        logger.error("[analysis] Fix generation failed: %s", exc)
        return []


# ── Fix application ────────────────────────────────────────────────────────────

# Keys that require human approval — never auto-applied
_HUMAN_REVIEW_KEYS = {"planner_prompt_addendum", "director_style_addendum"}


def _apply_fixes(fixes: list[dict], batch_id: str, db) -> None:
    """Log all fixes. Auto-apply safe ones with confidence >= 0.85."""
    for fix in fixes:
        key = fix.get("target_key", "")
        new_val = fix.get("new_value")
        old_val = fix.get("old_value")
        confidence = fix.get("confidence", 0)

        safe_to_auto = key not in _HUMAN_REVIEW_KEYS
        will_apply = confidence >= 0.85 and safe_to_auto

        db.add_fix_log(
            batch_id=batch_id,
            fix_type="config_change",
            target_key=key,
            old_value=json.dumps(old_val),
            new_value=json.dumps(new_val),
            notes=fix.get("rationale", ""),
            applied=will_apply,
        )

        if will_apply:
            db.upsert_system_config(
                key=key,
                value=json.dumps(new_val),
                updated_by=batch_id,
            )
            logger.info("[analysis] Auto-applied: %s = %s (confidence=%.2f)", key, new_val, confidence)
        elif confidence >= 0.70:
            logger.info("[analysis] Pending review: %s (confidence=%.2f)", key, confidence)


# ── Midnight scheduler (registered at startup) ────────────────────────────────

async def daily_analysis_loop() -> None:
    """Async loop: three nightly jobs running concurrently.
      00:05 UTC — feedback analysis
      00:10 UTC — PM insights (needs analysis results in DB)
      07:59 UTC — token usage report to Telegram (= 23:59 PST)
    """
    import asyncio

    async def _sleep_until(hour: int, minute: int) -> None:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

    async def _feedback_job():
        while True:
            await _sleep_until(0, 5)
            try:
                await asyncio.to_thread(run_daily_analysis)
            except Exception as exc:
                logger.error("[analysis] Daily feedback analysis failed: %s", exc)

    async def _pm_insights_job():
        while True:
            await _sleep_until(0, 10)
            try:
                from ai_team.pm_insights import run as run_pm_insights
                await asyncio.to_thread(run_pm_insights)
            except Exception as exc:
                logger.error("[analysis] PM insights failed: %s", exc)

    async def _token_report_job():
        """23:59 PST = 07:59 UTC next day."""
        while True:
            await _sleep_until(7, 59)
            try:
                from web.token_tracker import send_daily_token_report
                await asyncio.to_thread(send_daily_token_report)
            except Exception as exc:
                logger.error("[analysis] Token report failed: %s", exc)

    await asyncio.gather(_feedback_job(), _pm_insights_job(), _token_report_job())


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from dotenv import load_dotenv
    load_dotenv()

    import agent.deps as deps
    deps.init()

    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_daily_analysis(date_arg)
    print(json.dumps(result, indent=2, ensure_ascii=False))
