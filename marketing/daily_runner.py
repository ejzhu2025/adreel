"""marketing/daily_runner.py — daily agent: pick 10 brands, generate ads, notify Telegram."""
from __future__ import annotations

import os
import random
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Brand pool (10 categories, 5 brands each) ─────────────────────────────────
# Mix of large / medium / small to track which converts best.

BRAND_POOL: dict[str, list[dict]] = {
    "fashion": [
        {"url": "https://allbirds.com",       "size": "medium"},
        {"url": "https://gymshark.com",        "size": "medium"},
        {"url": "https://vuoriclothing.com",   "size": "medium"},
        {"url": "https://huckberry.com",       "size": "small"},
        {"url": "https://everlane.com",        "size": "medium"},
    ],
    "beauty": [
        {"url": "https://glossier.com",        "size": "large"},
        {"url": "https://rarebeauty.com",      "size": "large"},
        {"url": "https://byhumankind.com",     "size": "small"},
        {"url": "https://naturium.com",        "size": "medium"},
        {"url": "https://cocokind.com",        "size": "small"},
    ],
    "fitness": [
        {"url": "https://whoop.com",           "size": "large"},
        {"url": "https://therabody.com",       "size": "large"},
        {"url": "https://momentous.com",       "size": "medium"},
        {"url": "https://equinox.com",         "size": "large"},
        {"url": "https://tonal.com",           "size": "medium"},
    ],
    "food_beverage": [
        {"url": "https://drinkag1.com",        "size": "large"},
        {"url": "https://olipop.com",          "size": "medium"},
        {"url": "https://liquid-iv.com",       "size": "large"},
        {"url": "https://goodles.com",         "size": "small"},
        {"url": "https://Mid-Day Squares.com", "size": "small"},
    ],
    "home": [
        {"url": "https://parachutehome.com",   "size": "medium"},
        {"url": "https://brooklinen.com",      "size": "medium"},
        {"url": "https://thelightphone.com",   "size": "small"},
        {"url": "https://ugmonk.com",          "size": "small"},
        {"url": "https://tuftandneedle.com",   "size": "medium"},
    ],
    "tech": [
        {"url": "https://notion.so",           "size": "large"},
        {"url": "https://superhuman.com",      "size": "medium"},
        {"url": "https://arc.net",             "size": "medium"},
        {"url": "https://raycast.com",         "size": "small"},
        {"url": "https://linear.app",          "size": "medium"},
    ],
    "skincare": [
        {"url": "https://cerave.com",          "size": "large"},
        {"url": "https://topicals.com",        "size": "small"},
        {"url": "https://versed.com",          "size": "small"},
        {"url": "https://humanrace.com",       "size": "medium"},
        {"url": "https://twentytwentytwo.com", "size": "small"},
    ],
    "wellness": [
        {"url": "https://calm.com",            "size": "large"},
        {"url": "https://ritual.com",          "size": "medium"},
        {"url": "https://seed.com",            "size": "medium"},
        {"url": "https://eight-sleep.com",     "size": "medium"},
        {"url": "https://joovv.com",           "size": "small"},
    ],
    "pets": [
        {"url": "https://barkbox.com",         "size": "large"},
        {"url": "https://wildearth.com",       "size": "small"},
        {"url": "https://petaluma.pet",        "size": "small"},
        {"url": "https://forthglade.com",      "size": "small"},
        {"url": "https://yandc.com",           "size": "small"},
    ],
    "sustainable": [
        {"url": "https://tentree.com",         "size": "medium"},
        {"url": "https://patagonia.com",       "size": "large"},
        {"url": "https://girlfriend.com",      "size": "medium"},
        {"url": "https://naadam.co",           "size": "small"},
        {"url": "https://kotn.com",            "size": "small"},
    ],
}


def pick_daily_brands(n: int = 10) -> list[dict]:
    """Pick one brand from each category, shuffle and return n."""
    picks = []
    categories = list(BRAND_POOL.keys())
    random.shuffle(categories)
    for cat in categories[:n]:
        brand = random.choice(BRAND_POOL[cat])
        picks.append({**brand, "category": cat})
    return picks


def run_daily(
    n: int = 10,
    platforms: list[str] | None = None,
    notify: bool = True,
) -> None:
    """Main daily job: generate n ads, notify Telegram."""
    from marketing.brand_finder import BrandLead
    from marketing.campaign_runner import run_campaign
    from marketing.tracker import Tracker
    from marketing.notifier import notify_campaign, notify_daily_summary

    if platforms is None:
        platforms = ["tiktok", "instagram"]

    today = date.today().isoformat()
    brands = pick_daily_brands(n)
    tracker = Tracker()
    results = []

    print(f"\n{'='*50}")
    print(f"Adreel Daily Run — {today}")
    print(f"Brands: {n} | Platforms: {', '.join(platforms)}")
    print(f"{'='*50}\n")

    for i, brand_info in enumerate(brands, 1):
        print(f"\n[{i}/{n}] {brand_info['url']} ({brand_info['category']})")
        lead = BrandLead(
            url=brand_info["url"],
            size=brand_info["size"],
            category=brand_info["category"],
            source="daily_pool",
        )
        result = run_campaign(
            lead,
            platforms=platforms,
            quality="turbo",
            tracker=tracker,
        )
        results.append(result)

        if result.ok:
            print(f"  ✓ {result.brand} → {result.output_dir}")
            if notify:
                notify_campaign(result, result.copy)
        else:
            print(f"  ✗ {result.error}")

    # End-of-day summary
    if notify:
        notify_daily_summary(results, today)

    ok = sum(1 for r in results if r.ok)
    print(f"\n{'='*50}")
    print(f"Done: {ok}/{n} succeeded")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    run_daily()
