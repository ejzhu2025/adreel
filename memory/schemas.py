"""Pydantic schemas for all data structures in video-agent-hero."""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


# ── Brand Kit ─────────────────────────────────────────────────────────────────

class LogoConfig(BaseModel):
    path: str = "assets/tong_sui_logo.png"
    safe_area: str = "top_right"  # top_right | top_left | bottom_right | bottom_left


class ColorPalette(BaseModel):
    primary: str = "#00B894"
    secondary: str = "#FFFFFF"
    accent: str = "#FF7675"
    background: str = "#1A1A2E"


class FontConfig(BaseModel):
    title: str = "Poppins-SemiBold"
    body: str = "Inter-Regular"


class SubtitleStyle(BaseModel):
    position: str = "bottom_center"
    box_opacity: float = 0.55
    box_radius: int = 12
    padding_px: int = 14
    max_chars_per_line: int = 18
    highlight_keywords: bool = True
    font_size: int = 38


class IntroOutro(BaseModel):
    intro_template: str = "mint_splash"
    outro_cta: str = "Order now"
    intro_duration_sec: float = 1.5
    outro_duration_sec: float = 2.0


class BrandKit(BaseModel):
    brand_id: str
    name: str = ""
    logo: LogoConfig = Field(default_factory=LogoConfig)
    colors: ColorPalette = Field(default_factory=ColorPalette)
    fonts: FontConfig = Field(default_factory=FontConfig)
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    intro_outro: IntroOutro = Field(default_factory=IntroOutro)


# ── User Preferences ──────────────────────────────────────────────────────────

class UserPrefs(BaseModel):
    user_id: str
    default_platform: str = "tiktok"
    preferred_duration_sec: int = 20
    tone: list[str] = Field(default_factory=lambda: ["fresh", "playful"])
    pacing: str = "fast"
    shot_density: int = 7
    cta_style: str = "soft"


# ── Plan (produced by planner_llm) ────────────────────────────────────────────

class Script(BaseModel):
    hook: str
    body: list[str]
    cta: str


class StoryboardScene(BaseModel):
    scene: int
    desc: str
    duration: float  # seconds
    asset_hint: Optional[str] = None  # e.g. "product macro", "lifestyle"


class Shot(BaseModel):
    shot_id: str
    type: str  # macro | wide | close | text | transition
    asset: str  # asset key or "generate"
    text_overlay: str = ""
    duration: Optional[float] = None


class Plan(BaseModel):
    project_id: str
    brief: str = ""
    platform: str = "tiktok"
    duration_sec: int = 20
    language: str = "en"
    style_tone: list[str] = Field(default_factory=lambda: ["fresh"])
    script: Script
    storyboard: list[StoryboardScene]
    shot_list: list[Shot]
    render_targets: list[str] = Field(default_factory=lambda: ["9:16"])
    version: int = 1


# ── Caption segment ───────────────────────────────────────────────────────────

class CaptionSegment(BaseModel):
    index: int
    start_sec: float
    end_sec: float
    text: str
    highlighted_words: list[str] = Field(default_factory=list)


# ── Quality check result ──────────────────────────────────────────────────────

class QualityCheckResult(BaseModel):
    passed: bool
    duration_ok: bool = True
    captions_ok: bool = True
    logo_ok: bool = True
    issues: list[str] = Field(default_factory=list)
    auto_fix_applied: bool = False
    attempt: int = 1


# ── Project record ────────────────────────────────────────────────────────────

class ProjectRecord(BaseModel):
    project_id: str
    brief: str
    brand_id: str = "default"
    user_id: str = "default"
    created_at: str = ""
    updated_at: str = ""
    latest_plan_json: Optional[dict[str, Any]] = None
    output_paths: list[str] = Field(default_factory=list)
    status: str = "pending"  # pending | running | done | failed
