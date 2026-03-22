"""ai_team/pm_insights.py — Proactive PM: synthesize signals → draft requirements.

Runs daily. Reads four signal sources:
  1. Feedback analysis results already stored in DB (pain points, quotes, trend)
  2. Cloud Run error logs from last 24h
  3. User behavior patterns (project funnel, conversion, failure points)
  4. Recent git log (what shipped, what changed)

Output: reports/pm_insights_YYYY-MM-DD.md
  - UX problems automatically discovered (with evidence + severity)
  - Feature requests inferred from behavior
  - P0–P3 priority triage
  - Ready-to-implement user story cards (with acceptance criteria)

Usage:
  python -m ai_team.pm_insights             # today's report
  python -m ai_team.pm_insights --date 2026-03-20
  python -m ai_team.pm_insights --dry-run   # print signals without calling Claude
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = "adreel-490423"
SERVICE = "ads-video-hero"
REGION  = "us-central1"
REPORTS_DIR = Path(__file__).parent.parent / "reports"


# ── Signal collectors ──────────────────────────────────────────────────────────

def _collect_feedback_analysis(db) -> dict:
    """Read the most recent feedback analysis reports from DB."""
    try:
        recent = db.get_recent_analyses(limit=7)
        if not recent:
            return {"available": False, "reason": "No feedback analysis yet"}
        summaries = []
        pain_points_all = []
        for a in recent:
            rj = a.get("report_json") or {}
            summaries.append({
                "date": a.get("analysis_date"),
                "feedback_count": a.get("feedback_count", 0),
                "summary": rj.get("executive_summary", ""),
                "trend": rj.get("trend", ""),
                "priority_action": rj.get("priority_action", ""),
            })
            for pp in rj.get("top_pain_points", []):
                pp["date"] = a.get("analysis_date")
                pain_points_all.append(pp)
        # Deduplicate pain points by title (keep highest severity)
        seen: dict[str, dict] = {}
        for pp in pain_points_all:
            title = pp.get("title", "")
            if title not in seen or pp.get("severity", 0) > seen[title].get("severity", 0):
                seen[title] = pp
        return {
            "available": True,
            "days_analyzed": len(recent),
            "total_feedback": sum(s["feedback_count"] for s in summaries),
            "recent_trend": recent[0].get("report_json", {}).get("trend", "unknown") if recent else "unknown",
            "daily_summaries": summaries[:3],
            "recurring_pain_points": sorted(seen.values(), key=lambda x: -x.get("severity", 0))[:8],
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def _collect_behavior(db) -> dict:
    """Query user behavior and conversion funnel from DB."""
    def _q(conn, sql):
        try:
            cur = conn.execute(sql)
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    try:
        with db._conn() as conn:
            status_dist  = _q(conn, "SELECT status, COUNT(*) as cnt FROM projects GROUP BY status")
            projects_7d  = _q(conn, "SELECT COUNT(*) as cnt FROM projects WHERE created_at > datetime('now', '-7 days')")
            users_total  = _q(conn, "SELECT COUNT(*) as cnt FROM users")
            users_7d     = _q(conn, "SELECT COUNT(*) as cnt FROM users WHERE created_at > datetime('now', '-7 days')")
            fb_total     = _q(conn, "SELECT COUNT(*) as cnt FROM feedback")
            fb_rated     = _q(conn, "SELECT COUNT(*) as cnt, AVG(rating_overall) as avg_rating FROM feedback WHERE rating_overall IS NOT NULL")
            by_day       = _q(conn, "SELECT DATE(created_at) as day, COUNT(*) as cnt FROM projects WHERE created_at > datetime('now', '-14 days') GROUP BY day ORDER BY day DESC")
            failed       = _q(conn, "SELECT COUNT(*) as cnt FROM projects WHERE status IN ('error', 'failed')")

        total_p = sum(r.get("cnt", 0) for r in status_dist)
        done_p  = next((r.get("cnt", 0) for r in status_dist if r.get("status") == "done"), 0)
        error_p = failed[0].get("cnt", 0) if failed else 0

        return {
            "available": True,
            "funnel": {
                "total_projects": total_p,
                "completed": done_p,
                "failed": error_p,
                "completion_rate_pct": round(done_p / total_p * 100, 1) if total_p else 0,
                "failure_rate_pct": round(error_p / total_p * 100, 1) if total_p else 0,
            },
            "growth": {
                "new_projects_7d": projects_7d[0].get("cnt", 0) if projects_7d else 0,
                "new_users_7d": users_7d[0].get("cnt", 0) if users_7d else 0,
                "total_users": users_total[0].get("cnt", 0) if users_total else 0,
            },
            "feedback": {
                "total": fb_total[0].get("cnt", 0) if fb_total else 0,
                "avg_rating": round((fb_rated[0].get("avg_rating") or 0), 2) if fb_rated else 0,
            },
            "project_status_dist": status_dist,
            "daily_activity": by_day,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def _collect_errors(hours: int = 24) -> dict:
    """Fetch Cloud Run error logs from last N hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd = [
        "gcloud", "logging", "read",
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICE}"'
        f' AND timestamp>="{since}"',
        f"--project={PROJECT}",
        "--limit=500",
        "--format=json",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"available": False, "reason": result.stderr[:200]}
        entries = json.loads(result.stdout or "[]")

        error_lines = []
        status_counts: dict[str, int] = {}
        for e in entries:
            text = e.get("textPayload") or e.get("jsonPayload", {}).get("message", "")
            if not text:
                continue
            # Count HTTP status codes
            import re
            m = re.search(r'HTTP/1\.\d" (\d{3})', text)
            if m:
                code = m.group(1)
                status_counts[code] = status_counts.get(code, 0) + 1
            # Collect errors
            low = text.lower()
            if any(p in low for p in ["error", "exception", "traceback", "critical"]):
                if "deprecationwarning" not in low:
                    error_lines.append(text[:200])

        # Deduplicate similar errors (keep first occurrence of each pattern)
        deduped: list[str] = []
        seen_prefixes: set[str] = set()
        for line in error_lines:
            prefix = line[:60]
            if prefix not in seen_prefixes:
                seen_prefixes.add(prefix)
                deduped.append(line)

        return {
            "available": True,
            "hours_analyzed": hours,
            "total_log_lines": len(entries),
            "error_count": len(error_lines),
            "unique_errors": deduped[:15],
            "http_status_counts": status_counts,
            "error_rate_pct": round(len(error_lines) / len(entries) * 100, 1) if entries else 0,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def _collect_git_log(days: int = 14) -> dict:
    """Get recent git commits."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days} days ago",
             "--pretty=format:%ad|%s", "--date=short", "--no-merges", "-30"],
            capture_output=True, text=True, timeout=10,
            cwd=Path(__file__).parent.parent,
        )
        if result.returncode != 0:
            return {"available": False, "reason": "not a git repo"}
        commits = []
        for line in result.stdout.splitlines():
            date, sep, msg = line.partition("|")
            if sep:
                commits.append({"date": date.strip(), "message": msg.strip()})
        return {"available": True, "commits": commits, "count": len(commits)}
    except Exception as e:
        return {"available": False, "reason": str(e)}


# ── Claude synthesis ───────────────────────────────────────────────────────────

_PROMPT = """\
You are the PM for adreel.studio — an AI short-video ad creation SaaS (solo founder product).
Today's date: {date}

Synthesize the four signals into a concise PM report. Be specific, reference actual data.
Keep the entire report under 600 words. Cut anything vague or obvious.

SIGNAL 1 — FEEDBACK (last 7d): {feedback}
SIGNAL 2 — BEHAVIOR/FUNNEL: {behavior}
SIGNAL 3 — ERRORS (last 24h): {errors}
SIGNAL 4 — SHIPPED CODE (last 14d): {git_log}

Write in Markdown with these sections (keep each section tight):

## Summary
1-2 sentences: what's the most important thing happening right now.

## Top Issues
Max 3 issues. For each:
**[P0/P1/P2] Title** — one line evidence + one line fix.

## Priority Actions
A simple numbered list of the top 3 things to do next, most important first.
Each item: what to do + why (one sentence each).

## Top User Story
One story only — the highest-value thing to build next.
**As a** user **I want** X **so that** Y.
Acceptance criteria: 2-3 bullet points max.
"""


def _call_claude(signals: dict, date_str: str) -> str:
    """Call Claude Sonnet to synthesize signals into PM report."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return "ERROR: ANTHROPIC_API_KEY not set"

    client = anthropic.Anthropic(api_key=api_key)

    def _fmt(data: dict) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    prompt = _PROMPT.format(
        date=date_str,
        feedback=_fmt(signals["feedback"]),
        behavior=_fmt(signals["behavior"]),
        errors=_fmt(signals["errors"]),
        git_log=_fmt(signals["git"]),
    )

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        final_msg = stream.get_final_message()
        try:
            from web.token_tracker import log_tokens
            log_tokens("claude-sonnet-4-6", "pm_insights", final_msg.usage)
        except Exception:
            pass
        return final_msg.content[0].text


# ── Report writer ──────────────────────────────────────────────────────────────

def _write_report(content: str, date_str: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"pm_insights_{date_str}.md"
    header = f"# PM Insights — {date_str}\n\n_Auto-generated by ai_team/pm_insights.py_\n\n"
    path.write_text(header + content, encoding="utf-8")
    return path


# ── Telegram notification ──────────────────────────────────────────────────────

def _summarize_for_telegram(report_md: str, date_str: str) -> str:
    """Use Claude Haiku to produce a short Chinese summary for Telegram."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return report_md[:800]

    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""以下是今日 PM 洞察报告（英文）。请用中文写一份简短的 Telegram 推送，控制在 600 字以内。

格式要求：
- 第一行：📊 PM日报 {date_str}
- 用 2-3 句话概括今日核心发现
- 列出最重要的 2-3 个问题（每条一行，用 ⚠️ 标注严重问题，📌 标注一般问题）
- 列出今日最优先要做的 1 件事（用 ✅ 开头）
- 结尾加一句用户增长或关键数据（用 📈 开头）

不要写废话，不要重复，直接给结论。

报告原文：
{report_md[:3000]}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            from web.token_tracker import log_tokens
            log_tokens("claude-haiku-4-5-20251001", "telegram_summary", resp.usage)
        except Exception:
            pass
        return resp.content[0].text.strip()
    except Exception as exc:
        print(f"[pm_insights] Summary generation failed: {exc}")
        return report_md[:800]


def _send_telegram(report_md: str, date_str: str) -> None:
    """Send a short Chinese PM summary to Telegram. Requires TELEGRAM_BOT_TOKEN env var."""
    import urllib.request

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "8410200079").strip()

    if not bot_token:
        print("[pm_insights] TELEGRAM_BOT_TOKEN not set — skipping Telegram notification")
        return

    print("[pm_insights] Generating Chinese summary for Telegram...")
    text = _summarize_for_telegram(report_md, date_str)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("[pm_insights] Telegram message sent ✓")
            else:
                print(f"[pm_insights] Telegram error: {result}")
    except Exception as exc:
        print(f"[pm_insights] Telegram send failed: {exc}")


# ── Main entry point ───────────────────────────────────────────────────────────

def run(date_str: str | None = None, dry_run: bool = False) -> Path | None:
    """Collect signals, call Claude, write report. Returns report path."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from dotenv import load_dotenv
    load_dotenv()
    import agent.deps as deps
    deps.init()
    db = deps.db()

    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[pm_insights] Collecting signals for {date_str}...")

    signals = {
        "feedback": _collect_feedback_analysis(db),
        "behavior": _collect_behavior(db),
        "errors":   _collect_errors(hours=24),
        "git":      _collect_git_log(days=14),
    }

    # Show signal summary
    fb  = signals["feedback"]
    beh = signals["behavior"]
    err = signals["errors"]
    git = signals["git"]
    print(f"  Feedback: {fb.get('total_feedback', 0)} items, {fb.get('days_analyzed', 0)} days")
    print(f"  Behavior: {beh.get('funnel', {}).get('total_projects', '?')} projects, "
          f"{beh.get('funnel', {}).get('completion_rate_pct', '?')}% completion rate")
    print(f"  Errors:   {err.get('error_count', 0)} in last 24h "
          f"({err.get('total_log_lines', 0)} total log lines)")
    print(f"  Git:      {git.get('count', 0)} commits in last 14 days")

    if dry_run:
        print("\n[dry-run] Signals collected. Skipping Claude call.")
        print(json.dumps(signals, indent=2, ensure_ascii=False)[:3000])
        return None

    print(f"[pm_insights] Calling Claude Sonnet to synthesize...")
    report_md = _call_claude(signals, date_str)

    path = _write_report(report_md, date_str)
    print(f"[pm_insights] Report saved: {path}")
    print("\n" + "=" * 60)
    print(report_md)
    print("=" * 60)

    _send_telegram(report_md, date_str)

    return path


def main():
    parser = argparse.ArgumentParser(description="PM insights generator")
    parser.add_argument("--date", help="Date (YYYY-MM-DD), default: today")
    parser.add_argument("--dry-run", action="store_true", help="Collect signals only, skip Claude")
    args = parser.parse_args()
    run(date_str=args.date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
