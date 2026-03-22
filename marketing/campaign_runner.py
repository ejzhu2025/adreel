"""marketing/campaign_runner.py — orchestrate: scrape → video → content package → tracker."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# Ensure ads_video_hero root is on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from marketing.brand_finder import BrandLead
from marketing.content_packager import build_content_package
from marketing.tracker import Tracker


@dataclass
class CampaignResult:
    campaign_id: str
    brand: str
    url: str
    video_path: str
    output_dir: str
    brand_info: dict[str, Any]
    copy: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _get_gemini_client():
    """Return Gemini client if GOOGLE_API_KEY is set, else None."""
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except Exception:
        return None


def _make_brand_kit(brand_info: dict[str, Any], brand_id: str) -> "Any":
    """Build a BrandKit from scraped brand_info."""
    from memory.schemas import BrandKit, LogoConfig, ColorPalette, FontConfig, SubtitleStyle, IntroOutro

    primary = brand_info.get("primary_color") or brand_info.get("brand_info", {}).get("primary_color", "#333333")
    logo_path = brand_info.get("logo_path") or brand_info.get("brand_info", {}).get("logo_path", "")

    return BrandKit(
        brand_id=brand_id,
        name=brand_info.get("brand_name", brand_id),
        logo=LogoConfig(path=logo_path, safe_area="top_right"),
        colors=ColorPalette(
            primary=primary,
            secondary="#FFFFFF",
            accent=primary,
            background="#111111",
        ),
        fonts=FontConfig(title="Poppins-SemiBold", body="Inter-Regular"),
        subtitle_style=SubtitleStyle(
            position="bottom_center",
            box_opacity=0.55,
            box_radius=12,
            padding_px=14,
            max_chars_per_line=18,
            highlight_keywords=True,
            font_size=44,
        ),
        intro_outro=IntroOutro(
            intro_template="mint_splash",
            outro_cta="Shop Now",
            intro_duration_sec=1.5,
            outro_duration_sec=2.0,
        ),
    )


def _build_brief(brand_info: dict[str, Any]) -> str:
    """Construct a video brief from scraped brand data."""
    brief = brand_info.get("brief", "")
    if brief:
        return brief
    name = brand_info.get("brand_name", "")
    product = brand_info.get("product_name", name)
    hook = brand_info.get("emotional_hook", "")
    features = ", ".join(brand_info.get("key_features", [])[:3])
    audience = brand_info.get("target_audience", "")
    parts = [f"Create a TikTok/Reels ad for {product}."]
    if hook:
        parts.append(hook)
    if features:
        parts.append(f"Key features: {features}.")
    if audience:
        parts.append(f"Target audience: {audience}.")
    return " ".join(parts)


def run_campaign(
    lead: BrandLead,
    data_dir: Path | None = None,
    output_base: Path | None = None,
    platforms: list[str] | None = None,
    quality: str = "turbo",
    duration_sec: int = 8,
    force_replicate: bool = False,
    tracker: Tracker | None = None,
) -> CampaignResult:
    """Run full pipeline for one brand lead.

    Steps:
      1. Scrape brand website
      2. Create BrandKit + project in DB
      3. Run ads_video_hero pipeline → generate video
      4. Build content package (covers + copy)
      5. Record in tracker
    """
    import re as _re

    if platforms is None:
        platforms = ["tiktok", "instagram"]

    data_dir = data_dir or Path(os.getenv("VAH_DATA_DIR", str(_ROOT / "data")))
    output_base = output_base or _ROOT / "marketing" / "output"

    # ── 1. Scrape ──────────────────────────────────────────────────────────────
    print(f"\n[campaign] Scraping {lead.url} ...")
    from web.scrape_product import scrape_product
    gemini = _get_gemini_client()
    try:
        brand_info = asyncio.run(scrape_product(lead.url, data_dir, gemini))
    except Exception as e:
        return CampaignResult("", lead.name or lead.url, lead.url, "", "", {}, error=f"Scrape failed: {e}")

    # Enrich lead with scraped data
    brand_name = brand_info.get("brand_name") or lead.name or "brand"
    brand_id = _re.sub(r"[^a-z0-9_]", "_", brand_name.lower())[:32]

    # ── 2. Brand kit + project ────────────────────────────────────────────────
    import agent.deps as deps
    deps.init(str(data_dir))
    db = deps.db()

    brand_kit = _make_brand_kit(brand_info, brand_id)
    db.upsert_brand_kit(brand_kit)

    brief = _build_brief(brand_info)
    project_id = db.create_project(brief=brief, brand_id=brand_id, user_id="marketing")

    # Product image
    product_image_path = brand_info.get("image_path", "")
    if product_image_path:
        proj_dir = data_dir / "projects" / project_id
        proj_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        dst = proj_dir / "product.png"
        try:
            shutil.copy2(product_image_path, dst)
            product_image_path = str(dst)
        except Exception:
            pass

    # ── 3. Run pipeline ───────────────────────────────────────────────────────
    print(f"[campaign] Generating video for '{brand_name}' (project {project_id}) ...")
    from agent.graph import build_graph

    # Force Replicate by temporarily hiding FAL_KEY (marketing only, restores after)
    _fal_backup = None
    if force_replicate:
        _fal_backup = os.environ.pop("FAL_KEY", None) or os.environ.pop("FAL_API_KEY", None)

    tone = brand_info.get("style_tone", ["fresh"])
    if isinstance(tone, str):
        tone = [tone]

    initial_state: dict = {
        "project_id": project_id,
        "brief": brief,
        "brand_id": brand_id,
        "user_id": "marketing",
        "brand_kit": brand_kit.model_dump(),
        "user_prefs": {},
        "similar_projects": [],
        "messages": [],
        "clarification_answers": {
            "platform": "tiktok",
            "duration_sec": duration_sec,
            "style_tone": tone,
            "language": brand_info.get("language", "en"),
            "assets_available": "product_image" if product_image_path else "none",
        },
        "plan_version": 0,
        "qc_attempt": 1,
        "needs_replan": False,
        "quality": quality,
        "product_image_path": product_image_path,
    }

    db.update_project_status(project_id, "running")
    try:
        graph = build_graph()
        result = graph.invoke(initial_state)
        video_path = result.get("output_path", "")
    except Exception as e:
        db.update_project_status(project_id, "failed")
        return CampaignResult(
            project_id, brand_name, lead.url, "", "", brand_info,
            error=f"Pipeline failed: {e}",
        )
    finally:
        # Restore FAL_KEY if we hid it
        if force_replicate and _fal_backup:
            os.environ["FAL_KEY"] = _fal_backup

    if not video_path or not Path(video_path).exists():
        return CampaignResult(
            project_id, brand_name, lead.url, "", "", brand_info,
            error="Pipeline returned no video",
        )

    # ── 4. Content package ────────────────────────────────────────────────────
    today = date.today().isoformat()
    output_dir = output_base / today / brand_id
    print(f"[campaign] Building content package → {output_dir}")
    package = build_content_package(video_path, brand_info, output_dir, platforms)

    # ── 5. Tracker ────────────────────────────────────────────────────────────
    if tracker is None:
        tracker = Tracker()
    campaign_id = tracker.record_campaign(
        brand=brand_name,
        url=lead.url,
        size=lead.size,
        category=lead.category or brand_info.get("product_category", ""),
        video_path=video_path,
        output_dir=str(output_dir),
        brief=brief,
        campaign_id=project_id,
    )

    return CampaignResult(
        campaign_id=campaign_id,
        brand=brand_name,
        url=lead.url,
        video_path=video_path,
        output_dir=str(output_dir),
        brand_info=brand_info,
        copy=package.get("copy", {}),
    )
