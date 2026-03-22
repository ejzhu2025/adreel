"""scripts/scrape_quality_test.py — Scrape 10 URLs, evaluate quality, send Telegram report."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

TEST_URLS = [
    # Shopify-based (static-friendly)
    ("Allbirds Wool Runner",   "https://www.allbirds.com/products/mens-wool-runners"),
    ("Gymshark Vital Seamless","https://www.gymshark.com/products/gymshark-vital-seamless-2-0-leggings-black-ss21"),
    # Large brand PDP (anti-bot)
    ("Nike Air Force 1",       "https://www.nike.com/t/air-force-1-07-mens-shoes-jBrhbr/CW2288-111"),
    ("Dyson Airwrap",          "https://www.dyson.com/hair-care/stylers/airwrap-multi-styler/complete-long-nickel-copper"),
    ("lululemon Align Pant",   "https://www.lululemon.com/en-us/p/align-high-rise-pant-28/LW5CXBS"),
    # Beauty / skincare PDP
    ("Sephora Retinol",        "https://www.sephora.com/product/the-ordinary-retinol-0-5-in-squalane-P460571"),
    ("CeraVe Moisturizer",     "https://www.cerave.com/moisturizing-cream"),
    # DTC / mid-size brand PDP
    ("Patagonia Sweater",      "https://www.patagonia.com/product/mens-better-sweater-fleece-jacket/25528.html"),
    ("Glossier Cloud Paint",   "https://www.glossier.com/products/cloud-paint"),
    ("Stanley Quencher",       "https://www.stanley1913.com/products/adventure-quencher-travel-tumbler-30-oz"),
]

_GARBAGE_TITLES = [
    "access denied", "403 forbidden", "404", "not found", "error",
    "just a moment", "attention required", "pardon our interruption",
    "hang tight", "routing to checkout", "captcha", "enable javascript",
    "please wait", "checking your browser",
]

def _is_garbage(title: str) -> bool:
    t = title.lower()
    return any(m in t for m in _GARBAGE_TITLES)


# ── Scrape one URL ─────────────────────────────────────────────────────────────

async def scrape_one(label: str, url: str, data_dir: Path, gemini_client) -> dict:
    from web.scrape_product import scrape_product, _jina_fetch, _extract_page_content
    import httpx

    start = time.time()

    # Probe L1 and L2 before running full pipeline (for routing classification)
    l1_title, l1_ok, l1_garbage = "", False, False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
            html = r.text
        content = _extract_page_content(html, url)
        l1_title = content.get("title", "")
        l1_ok = bool(l1_title or content.get("body_text", "").strip())
        l1_garbage = _is_garbage(l1_title)
    except Exception:
        l1_garbage = True

    jina_triggered = not l1_ok or l1_garbage
    jina_title, jina_ok = "", False
    if jina_triggered:
        try:
            jt = await _jina_fetch(url)
            if jt:
                jina_title = jt.split("\n")[0].lstrip("# ").strip()
                jina_ok = bool(jina_title) and not _is_garbage(jina_title)
        except Exception:
            pass

    # Determine routing source
    if not l1_garbage and l1_ok:
        source = "L1"
    elif jina_ok:
        source = "L2-Jina"
    else:
        source = "Brand-Intel"

    # Run full pipeline
    try:
        result = await scrape_product(url, data_dir, gemini_client=gemini_client)
        elapsed = round(time.time() - start, 1)
        return {
            "label": label, "url": url, "elapsed_s": elapsed,
            "status": "ok", "source": source,
            "mode": result.get("mode", "direct"),
            # Extracted fields
            "brand_name":      result.get("brand_name") or result.get("product_name", ""),
            "product_name":    result.get("product_name", ""),
            "category":        result.get("product_category", ""),
            "key_features":    result.get("key_features", []),
            "target_audience": result.get("target_audience", ""),
            "style_tone":      result.get("style_tone", []),
            "brief":           result.get("brief", ""),
            "image_url":       result.get("image_url", "") or result.get("logo_url", ""),
            "image_on_disk":   bool(result.get("image_path") and Path(result.get("image_path","")).exists())
                               or bool(result.get("logo_path") and Path(result.get("logo_path","")).exists()),
            # Routing detail
            "l1_title": l1_title, "l1_garbage": l1_garbage,
            "jina_triggered": jina_triggered, "jina_ok": jina_ok, "jina_title": jina_title,
        }
    except Exception as e:
        return {
            "label": label, "url": url, "elapsed_s": round(time.time()-start,1),
            "status": "error", "source": "failed", "mode": "failed", "error": str(e)[:100],
            "brand_name":"","product_name":"","category":"","key_features":[],"target_audience":"",
            "style_tone":[],"brief":"","image_url":"","image_on_disk":False,
            "l1_title":l1_title,"l1_garbage":l1_garbage,"jina_triggered":jina_triggered,
            "jina_ok":jina_ok,"jina_title":jina_title,
        }


# ── Quality evaluation via Claude Haiku ────────────────────────────────────────

def evaluate_quality(results: list[dict]) -> list[dict]:
    """Ask Claude Haiku to score each result across 6 dimensions (1-5)."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return results

    client = anthropic.Anthropic(api_key=api_key)

    items = []
    for r in results:
        if r["status"] != "ok":
            continue
        items.append({
            "label": r["label"],
            "brand_name": r["brand_name"],
            "product_name": r["product_name"],
            "category": r["category"],
            "target_audience": r["target_audience"],
            "style_tone": r["style_tone"],
            "brief": r["brief"],
            "has_image": r["image_on_disk"],
        })

    prompt = f"""You are a quality evaluator for an AI video ad platform's data pipeline.
Each item below was auto-scraped from a product URL. Score each field 1-5 where:
5 = accurate, specific, ready to use  |  3 = ok but vague  |  1 = wrong or missing

Items to evaluate:
{json.dumps(items, ensure_ascii=False, indent=2)}

Return ONLY a JSON array, one object per item, in the same order:
[{{
  "label": "<same label>",
  "scores": {{
    "brand_name": <1-5>,
    "product_name": <1-5>,
    "category": <1-5>,
    "target_audience": <1-5>,
    "style_tone": <1-5>,
    "brief": <1-5>
  }},
  "overall": <1-5>,
  "note": "<one sentence: biggest quality issue or praise>"
}}]"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            from web.token_tracker import log_tokens
            log_tokens("claude-haiku-4-5-20251001", "scrape_quality_eval", resp.usage)
        except Exception:
            pass
        text = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```")
        scores = json.loads(text)
        score_map = {s["label"]: s for s in scores}
        for r in results:
            if r["label"] in score_map:
                r["quality"] = score_map[r["label"]]
    except Exception as e:
        print(f"[quality_eval] Failed: {e}")

    return results


# ── Build report ───────────────────────────────────────────────────────────────

def build_report(results: list[dict]) -> str:
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "error"]
    avg_time = round(sum(r["elapsed_s"] for r in results) / len(results), 1)

    # Routing counts
    from collections import Counter
    routing = Counter(r["source"] for r in results)
    l1_count      = routing.get("L1", 0)
    jina_count    = routing.get("L2-Jina", 0)
    intel_count   = routing.get("Brand-Intel", 0)
    failed_count  = routing.get("failed", 0) + len(failed)

    # Field success counts (non-empty = success)
    def got(r, field):
        v = r.get(field, "")
        if isinstance(v, list): return len(v) > 0
        return bool(str(v).strip())

    fields = ["brand_name", "product_name", "category", "target_audience",
              "style_tone", "brief", "image_url", "image_on_disk"]
    field_labels = {
        "brand_name":      "品牌名",
        "product_name":    "产品名",
        "category":        "产品类别",
        "target_audience": "目标客群",
        "style_tone":      "艺术风格",
        "brief":           "视频 Brief",
        "image_url":       "图片 URL",
        "image_on_disk":   "图片已下载",
    }
    field_counts = {f: sum(1 for r in ok if got(r, f)) for f in fields}

    # Average quality scores
    scored = [r for r in ok if r.get("quality")]
    avg_scores = {}
    if scored:
        for field in ["brand_name","product_name","category","target_audience","style_tone","brief"]:
            vals = [r["quality"]["scores"].get(field,0) for r in scored]
            avg_scores[field] = round(sum(vals)/len(vals), 1)
        avg_overall = round(sum(r["quality"]["overall"] for r in scored)/len(scored), 1)
    else:
        avg_overall = 0

    lines = []

    # ── 1. 总体概况 ───────────────────────────────────────────────────────────
    lines += [
        "===== Scrape Quality Report =====",
        f"测试URL数: {len(results)}  成功: {len(ok)}  失败: {len(failed)}  平均耗时: {avg_time}s",
        "",
        "── 路由汇总 ──",
        f"L1 httpx 直接拿到:  {l1_count}/{len(results)} 个",
        f"L2 Jina 兜底拿到:   {jina_count}/{len(results)} 个",
        f"Brand Intelligence: {intel_count}/{len(results)} 个 (L1+L2 均失败)",
        f"完全失败:           {failed_count}/{len(results)} 个",
    ]

    # ── 2. 字段成功率汇总 ─────────────────────────────────────────────────────
    lines += ["", "── 字段提取成功率 (n=10) ──"]
    for f in fields:
        cnt = field_counts[f]
        bar = "+" * cnt + "-" * (len(ok) - cnt)
        avg_q = f"  质量均分 {avg_scores[f]}/5" if f in avg_scores else ""
        lines.append(f"  {field_labels[f]:<12}: {cnt}/{len(ok)}  [{bar}]{avg_q}")

    if avg_overall:
        lines.append(f"\n  综合质量均分: {avg_overall}/5 (Claude Haiku 评分)")

    # ── 3. 每条URL详情 ────────────────────────────────────────────────────────
    lines += ["", "── 逐条结果 ──"]
    for i, r in enumerate(results, 1):
        q = r.get("quality", {})
        overall = q.get("overall", "-")
        note = q.get("note", "")
        source_tag = {"L1":"[L1]","L2-Jina":"[L2]","Brand-Intel":"[AI]","failed":"[X]"}.get(r["source"],"[?]")
        lines.append(f"\n{i}. {r['label']} {source_tag}  {r['elapsed_s']}s  质量:{overall}/5")

        if r["status"] == "error":
            lines.append(f"   ERROR: {r.get('error','')}")
            continue

        # Routing detail
        if r["source"] == "L1":
            lines.append(f"   路由: L1 httpx 直接获取 (title正常)")
        elif r["source"] == "L2-Jina":
            lines.append(f"   路由: L1 title='{r['l1_title'][:30]}' [垃圾] → Jina成功 title='{r['jina_title'][:30]}'")
        elif r["source"] == "Brand-Intel":
            lines.append(f"   路由: L1+L2 均失败 → Claude brand intelligence 推断")

        # Field values
        scores = q.get("scores", {})
        def fs(field): return f" ({scores[field]}/5)" if field in scores else ""
        lines.append(f"   品牌: {r['brand_name'][:35]}{fs('brand_name')}")
        lines.append(f"   产品: {r['product_name'][:35]}{fs('product_name')}")
        lines.append(f"   类别: {r['category']}{fs('category')}  客群: {(r['target_audience'] or '无')[:30]}{fs('target_audience')}")
        lines.append(f"   风格: {r['style_tone']}{fs('style_tone')}")
        lines.append(f"   Brief: {r['brief'][:70]}...{fs('brief')}")
        img_status = "已下载" if r["image_on_disk"] else ("有URL" if r["image_url"] else "无图")
        lines.append(f"   图片: {img_status}")
        if note:
            lines.append(f"   评估: {note}")

    return "\n".join(lines)


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        payload = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read())
                print("[telegram]", "sent" if res.get("ok") else res)
        except Exception as e:
            print(f"[telegram] Failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    from agent.nodes.planner_llm import get_gemini_client
    gemini_client = get_gemini_client()
    print(f"[scrape_test] Gemini: {'OK' if gemini_client else 'NOT AVAILABLE'}")

    data_dir = Path(os.getenv("VAH_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, (label, url) in enumerate(TEST_URLS, 1):
        print(f"[{i:2d}/10] {label}...")
        r = await scrape_one(label, url, data_dir, gemini_client)
        results.append(r)
        print(f"       source={r['source']} brand={r.get('brand_name','')[:30]!r} {r['elapsed_s']}s")

    print("\n[quality] Running Claude Haiku quality evaluation...")
    results = evaluate_quality(results)

    report = build_report(results)
    print("\n" + report)
    send_telegram(report)


if __name__ == "__main__":
    asyncio.run(main())
