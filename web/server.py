"""web/server.py — FastAPI app setup, startup, settings, and HTML serving."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from dotenv import load_dotenv

# Load .env from project root, then from the persistent data volume (HF Spaces).
# The data-volume copy takes precedence so user-saved keys survive container rebuilds.
load_dotenv()
_data_env = Path(os.environ.get("VAH_DATA_DIR", "./data")) / ".env"
if _data_env.exists():
    load_dotenv(_data_env, override=True)

# Add project root to path so we can import agent.*
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import agent.deps as deps
from web.brand_kit_api import router as brand_kit_router
from web.auth.router import router as auth_router
from web.billing.router import router as billing_router
from web.feedback_api import router as feedback_router
from web.routers.projects import router as projects_router
from web.templates import _HTML

app = FastAPI(title="Video Agent Hero")
app.include_router(brand_kit_router)
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(feedback_router)
app.include_router(projects_router)


# ── Startup ───────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    deps.init()
    # Start midnight feedback analysis loop
    asyncio.create_task(_start_analysis_loop())
    # Auto-seed brand kit + user prefs so the app works out of the box
    try:
        from memory.schemas import (
            BrandKit, UserPrefs, LogoConfig, ColorPalette, FontConfig,
            SubtitleStyle, IntroOutro,
        )
        from scripts.create_assets import create_placeholder_logo
        db = deps.db()
        logo_path = create_placeholder_logo()
        tong_sui = BrandKit(
            brand_id="tong_sui", name="Tong Sui",
            logo=LogoConfig(path=str(logo_path), safe_area="top_right"),
            colors=ColorPalette(primary="#00B894", secondary="#FFFFFF",
                                accent="#FF7675", background="#1A1A2E"),
            fonts=FontConfig(title="Poppins-SemiBold", body="Inter-Regular"),
            subtitle_style=SubtitleStyle(
                position="bottom_center", box_opacity=0.55, box_radius=12,
                padding_px=14, max_chars_per_line=18, highlight_keywords=True, font_size=44,
            ),
            intro_outro=IntroOutro(
                intro_template="mint_splash", outro_cta="Order now",
                intro_duration_sec=1.5, outro_duration_sec=2.0,
            ),
        )
        if not db.get_brand_kit("tong_sui"):
            db.upsert_brand_kit(tong_sui)
        ej = UserPrefs(
            user_id="ej", default_platform="tiktok", preferred_duration_sec=20,
            tone=["fresh", "playful", "premium"], pacing="fast", shot_density=7, cta_style="soft",
        )
        if not db.get_user_prefs("ej"):
            db.upsert_user_prefs(ej)
    except Exception as e:
        print(f"[startup] brand kit seed skipped: {e}", flush=True)


async def _start_analysis_loop():
    from web.feedback_analysis import daily_analysis_loop
    await daily_analysis_loop()


# ── Request models ────────────────────────────────────────────────────────────


class ApiKeyRequest(BaseModel):
    anthropic_api_key: str
    fal_key: str = ""
    replicate_api_token: str = ""


# ── Settings & utility endpoints ──────────────────────────────────────────────


@app.post("/api/init")
async def init_db():
    """Initialize DB with sample Tong Sui brand kit."""
    from memory.schemas import (
        BrandKit, UserPrefs, LogoConfig, ColorPalette, FontConfig,
        SubtitleStyle, IntroOutro,
    )
    from scripts.create_assets import create_placeholder_logo

    db = deps.db()
    logo_path = create_placeholder_logo()
    tong_sui = BrandKit(
        brand_id="tong_sui",
        name="Tong Sui",
        logo=LogoConfig(path=str(logo_path), safe_area="top_right"),
        colors=ColorPalette(
            primary="#00B894", secondary="#FFFFFF",
            accent="#FF7675", background="#1A1A2E",
        ),
        fonts=FontConfig(title="Poppins-SemiBold", body="Inter-Regular"),
        subtitle_style=SubtitleStyle(
            position="bottom_center", box_opacity=0.55, box_radius=12,
            padding_px=14, max_chars_per_line=18, highlight_keywords=True, font_size=44,
        ),
        intro_outro=IntroOutro(
            intro_template="mint_splash", outro_cta="Order now",
            intro_duration_sec=1.5, outro_duration_sec=2.0,
        ),
    )
    db.upsert_brand_kit(tong_sui)
    ej = UserPrefs(
        user_id="ej", default_platform="tiktok", preferred_duration_sec=20,
        tone=["fresh", "playful", "premium"], pacing="fast", shot_density=7, cta_style="soft",
    )
    db.upsert_user_prefs(ej)
    return {"status": "ok", "message": "DB initialized with Tong Sui brand kit"}


def _mask_key(key: str) -> str:
    return f"{key[:8]}…{key[-4:]}" if len(key) > 12 else ("set" if key else "")


def _upsert_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Write/update key=value pairs in a .env file without touching other lines."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    for var in updates:
        lines = [l for l in lines if not l.startswith(f"{var}=")]
    for var, val in updates.items():
        lines.append(f"{var}={val}")
    env_path.write_text("\n".join(lines) + "\n")


@app.get("/api/settings")
async def get_settings():
    """Return current settings (keys masked)."""
    ant_key = os.environ.get("ANTHROPIC_API_KEY", "")
    fal_key = os.environ.get("FAL_KEY", "") or os.environ.get("FAL_API_KEY", "")
    rep_token = os.environ.get("REPLICATE_API_TOKEN", "")
    return {
        "anthropic_api_key_set": bool(ant_key),
        "anthropic_api_key_preview": _mask_key(ant_key),
        "fal_key_set": bool(fal_key),
        "fal_key_preview": _mask_key(fal_key),
        "replicate_api_token_set": bool(rep_token),
        "replicate_api_token_preview": _mask_key(rep_token),
    }


@app.post("/api/settings")
async def save_settings(req: ApiKeyRequest):
    """Set API keys for this session and persist to .env."""
    ant_key = req.anthropic_api_key.strip()
    fal_key = req.fal_key.strip()
    if not ant_key and not fal_key:
        raise HTTPException(status_code=400, detail="At least one API key must be provided")
    data_dir = Path(os.environ.get("VAH_DATA_DIR", str(Path(__file__).parent.parent / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    env_path = data_dir / ".env"
    updates: dict[str, str] = {}
    replicate_token = req.replicate_api_token.strip()
    if ant_key:
        os.environ["ANTHROPIC_API_KEY"] = ant_key
        updates["ANTHROPIC_API_KEY"] = ant_key
    if fal_key:
        os.environ["FAL_KEY"] = fal_key
        updates["FAL_KEY"] = fal_key
    if replicate_token:
        os.environ["REPLICATE_API_TOKEN"] = replicate_token
        updates["REPLICATE_API_TOKEN"] = replicate_token
    _upsert_env_file(env_path, updates)
    return {
        "status": "ok",
        "anthropic_preview": _mask_key(ant_key) if ant_key else None,
        "fal_preview": _mask_key(fal_key) if fal_key else None,
        "replicate_preview": _mask_key(replicate_token) if replicate_token else None,
    }


@app.get("/video/{filename}")
async def serve_video(filename: str):
    """Serve a video file from the exports directory."""
    if not filename.endswith(".mp4") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    data_dir = os.environ.get("VAH_DATA_DIR", str(Path(__file__).parent.parent / "data"))
    video_path = Path(data_dir) / "exports" / filename
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(video_path, media_type="video/mp4")


# ── HTML frontend ─────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
