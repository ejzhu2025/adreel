"""web/token_tracker.py — Log Claude API token usage and send daily Telegram summaries.

Usage:
    from web.token_tracker import log_tokens

    resp = client.messages.create(model=MODEL, ...)
    log_tokens(MODEL, "planner", resp.usage)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

# ── Pricing table (USD per million tokens) ────────────────────────────────────
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-6":           (5.00, 25.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00,  5.00),
    "claude-haiku-4-5":          (1.00,  5.00),
}
_CACHED_DISCOUNT = 0.1   # cached input costs 10% of normal input price


def _price(model: str, input_tok: int, output_tok: int, cached_tok: int) -> float:
    in_price, out_price = _PRICES.get(model, (3.00, 15.00))
    cost = (input_tok - cached_tok) / 1_000_000 * in_price
    cost += cached_tok / 1_000_000 * in_price * _CACHED_DISCOUNT
    cost += output_tok / 1_000_000 * out_price
    return cost


def log_tokens(model: str, purpose: str, usage) -> None:
    """Record token usage from an Anthropic API response.usage object.

    Never raises — logging must not break the main call path.
    """
    try:
        import agent.deps as deps
        db = deps.db()
        db.log_token_usage(
            model=model,
            purpose=purpose,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    except Exception:
        pass


def _pst_day_bounds() -> tuple[str, str]:
    """Return (start_iso, end_iso) in UTC for today in PST (UTC-8)."""
    now_utc = datetime.now(timezone.utc)
    pst_offset = timedelta(hours=8)
    now_pst = now_utc - pst_offset
    day_start_pst = now_pst.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_pst   = day_start_pst + timedelta(days=1)
    # Convert back to UTC ISO strings for DB query
    start_iso = (day_start_pst + pst_offset).strftime("%Y-%m-%dT%H:%M:%S")
    end_iso   = (day_end_pst   + pst_offset).strftime("%Y-%m-%dT%H:%M:%S")
    return start_iso, end_iso


def build_daily_summary() -> str:
    """Build a Chinese Telegram message with today's PST token usage."""
    import agent.deps as deps

    start_iso, end_iso = _pst_day_bounds()
    now_pst = (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")

    rows = deps.db().get_token_usage_since(start_iso)
    # Filter to today only (end_iso boundary)
    rows = [r for r in rows if r.get("model")]

    if not rows:
        return f"Claude Token 日报 — {now_pst}\n\n今日无 API 调用记录。"

    # Aggregate by model
    by_model: dict[str, dict] = {}
    by_purpose: dict[str, int] = {}
    total_cost = 0.0

    for r in rows:
        model   = r["model"]
        purpose = r["purpose"] or "other"
        inp     = r["input_tokens"]
        out     = r["output_tokens"]
        cached  = r["cached_tokens"]

        if model not in by_model:
            by_model[model] = {"calls": 0, "input": 0, "output": 0, "cached": 0, "cost": 0.0}
        by_model[model]["calls"]  += 1
        by_model[model]["input"]  += inp
        by_model[model]["output"] += out
        by_model[model]["cached"] += cached
        c = _price(model, inp, out, cached)
        by_model[model]["cost"]   += c
        total_cost += c

        by_purpose[purpose] = by_purpose.get(purpose, 0) + 1

    total_calls   = sum(v["calls"]  for v in by_model.values())
    total_input   = sum(v["input"]  for v in by_model.values())
    total_output  = sum(v["output"] for v in by_model.values())
    total_cached  = sum(v["cached"] for v in by_model.values())

    def _k(n: int) -> str:
        return f"{n/1000:.1f}k" if n >= 1000 else str(n)

    lines = [
        f"Claude Token 日报 — {now_pst}",
        f"调用 {total_calls} 次  |  估算花费 ${total_cost:.3f}",
        "",
        "模型明细:",
    ]
    for model, v in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
        short = model.replace("claude-", "").replace("-20251001", "")
        lines.append(
            f"  {short}: {v['calls']}次  "
            f"in:{_k(v['input'])} out:{_k(v['output'])}  ${v['cost']:.3f}"
        )

    lines += ["", "功能分布:"]
    _LABELS = {
        "planner": "视频规划", "feedback_analysis": "反馈分析",
        "pm_insights": "PM日报", "scrape_intel": "品牌识别",
        "telegram_summary": "Telegram摘要", "fix_generation": "自动修复",
        "category_mining": "分类挖掘",
    }
    for purpose, cnt in sorted(by_purpose.items(), key=lambda x: -x[1]):
        label = _LABELS.get(purpose, purpose)
        lines.append(f"  {label}: {cnt}次")

    if total_cached:
        saved = _price("claude-sonnet-4-6", total_cached, 0, 0) * (1 - _CACHED_DISCOUNT)
        lines += ["", f"缓存节省约 ${saved:.3f}  ({_k(total_cached)} tokens)"]

    return "\n".join(lines)


def send_daily_token_report() -> None:
    """Send today's token usage summary to Telegram at 23:59 PST."""
    import urllib.request, json

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "8410200079").strip()
    if not bot_token:
        return

    text = build_daily_summary()
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                print("[token_tracker] Daily report sent to Telegram")
            else:
                print(f"[token_tracker] Telegram error: {result}")
    except Exception as exc:
        print(f"[token_tracker] Telegram send failed: {exc}")
