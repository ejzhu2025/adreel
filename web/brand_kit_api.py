"""web/brand_kit_api.py — Brand Kit CRUD + logo upload + Clearbit fetch."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import agent.deps as deps
from memory.schemas import BrandKit, ColorPalette, LogoConfig

router = APIRouter()

_DATA_DIR = Path(os.environ.get("VAH_DATA_DIR", "./data"))
_BRAND_KIT_DIR = _DATA_DIR / "brand_kits"


def _kit_dir(brand_id: str) -> Path:
    d = _BRAND_KIT_DIR / brand_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── List ──────────────────────────────────────────────────────────────────────


@router.get("/api/brand-kits")
async def list_brand_kits():
    kits = deps.db().list_brand_kits()
    return [k.model_dump() for k in kits]


# ── Create ────────────────────────────────────────────────────────────────────


class CreateBrandKitRequest(BaseModel):
    name: str
    colors: dict = {}
    safe_area: str = "top_right"


@router.post("/api/brand-kits")
async def create_brand_kit(req: CreateBrandKitRequest):
    brand_id = uuid4().hex[:8]
    colors = ColorPalette(
        primary=req.colors.get("primary", "#00B894"),
        secondary=req.colors.get("secondary", "#FFFFFF"),
        accent=req.colors.get("accent", "#FF7675"),
        background=req.colors.get("background", "#1A1A2E"),
    )
    kit = BrandKit(
        brand_id=brand_id,
        name=req.name,
        logo=LogoConfig(path="", safe_area=req.safe_area),
        colors=colors,
    )
    deps.db().upsert_brand_kit(kit)
    return kit.model_dump()


# ── Update ────────────────────────────────────────────────────────────────────


class UpdateBrandKitRequest(BaseModel):
    name: Optional[str] = None
    colors: Optional[dict] = None
    safe_area: Optional[str] = None


@router.put("/api/brand-kits/{brand_id}")
async def update_brand_kit(brand_id: str, req: UpdateBrandKitRequest):
    kit = deps.db().get_brand_kit(brand_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Brand kit not found")
    if req.name is not None:
        kit.name = req.name
    if req.colors:
        kit.colors = ColorPalette(
            primary=req.colors.get("primary", kit.colors.primary),
            secondary=req.colors.get("secondary", kit.colors.secondary),
            accent=req.colors.get("accent", kit.colors.accent),
            background=req.colors.get("background", kit.colors.background),
        )
    if req.safe_area is not None:
        kit.logo.safe_area = req.safe_area
    deps.db().upsert_brand_kit(kit)
    return kit.model_dump()


# ── Delete ────────────────────────────────────────────────────────────────────


@router.delete("/api/brand-kits/{brand_id}")
async def delete_brand_kit(brand_id: str):
    kit = deps.db().get_brand_kit(brand_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Brand kit not found")
    deps.db().delete_brand_kit(brand_id)
    return {"status": "deleted"}


# ── Upload logo ───────────────────────────────────────────────────────────────


@router.post("/api/brand-kits/{brand_id}/logo")
async def upload_logo(brand_id: str, file: UploadFile = File(...)):
    kit = deps.db().get_brand_kit(brand_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Brand kit not found")

    content = await file.read()
    if len(content) < 10:
        raise HTTPException(status_code=400, detail="File too small — may be empty")

    suffix = Path(file.filename or "logo.png").suffix.lower() or ".png"
    logo_path = _kit_dir(brand_id) / f"logo{suffix}"
    logo_path.write_bytes(content)

    kit.logo.path = str(logo_path)
    deps.db().upsert_brand_kit(kit)
    return {"status": "ok", "path": str(logo_path)}


# ── Serve logo ────────────────────────────────────────────────────────────────


@router.get("/api/brand-kits/{brand_id}/logo")
async def get_logo(brand_id: str):
    kit = deps.db().get_brand_kit(brand_id)
    if not kit or not kit.logo.path:
        raise HTTPException(status_code=404, detail="No logo set")
    logo_path = Path(kit.logo.path)
    if not logo_path.exists():
        raise HTTPException(status_code=404, detail="Logo file not found on disk")
    return FileResponse(logo_path)


# ── Clearbit fetch ────────────────────────────────────────────────────────────


class FetchLogoRequest(BaseModel):
    url: str
    brand_id: str


@router.post("/api/brand-kits/fetch-logo")
async def fetch_logo(req: FetchLogoRequest):
    from urllib.parse import urlparse
    import io
    from PIL import Image

    kit = deps.db().get_brand_kit(req.brand_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Brand kit not found")

    raw = req.url.strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    domain = (parsed.netloc or parsed.path).lstrip("www.")
    if not domain:
        raise HTTPException(status_code=400, detail="Invalid URL")

    sources = [
        f"https://logo.clearbit.com/{domain}",
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
    ]

    def _try_fetch(url: str) -> bytes | None:
        try:
            r = httpx.get(url, timeout=8, follow_redirects=True)
            if r.status_code != 200:
                return None
            ct = r.headers.get("content-type", "")
            if "html" in ct:
                return None
            # Validate it's a real image
            Image.open(io.BytesIO(r.content)).verify()
            return r.content
        except Exception:
            return None

    content: bytes | None = None
    for src in sources:
        content = _try_fetch(src)
        if content:
            break

    if not content:
        raise HTTPException(status_code=400, detail="无法自动获取 logo，请手动上传图片")

    logo_path = _kit_dir(req.brand_id) / "logo.png"
    logo_path.write_bytes(content)

    kit.logo.path = str(logo_path)
    deps.db().upsert_brand_kit(kit)
    return {"status": "ok", "domain": domain, "path": str(logo_path)}
