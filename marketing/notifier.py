"""marketing/notifier.py — send daily campaign results to Telegram."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

_BOT_TOKEN = lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = lambda: os.getenv("TELEGRAM_CHAT_ID", "")
_API       = "https://api.telegram.org/bot{token}/{method}"


def _url(method: str) -> str:
    return _API.format(token=_BOT_TOKEN(), method=method)


def send_text(text: str) -> bool:
    if not _BOT_TOKEN() or not _CHAT_ID():
        return False
    try:
        r = httpx.post(_url("sendMessage"), json={
            "chat_id": _CHAT_ID(),
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"[notifier] sendMessage failed: {e}")
        return False


def send_video(video_path: str, caption: str = "") -> bool:
    """Send a video file to Telegram (max 50MB)."""
    if not _BOT_TOKEN() or not _CHAT_ID():
        return False
    path = Path(video_path)
    if not path.exists():
        print(f"[notifier] Video not found: {video_path}")
        return False
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > 50:
        print(f"[notifier] Video too large ({size_mb:.1f}MB), skipping")
        return False
    try:
        with open(path, "rb") as f:
            r = httpx.post(
                _url("sendVideo"),
                data={"chat_id": _CHAT_ID(), "caption": caption[:1024], "parse_mode": "Markdown"},
                files={"video": (path.name, f, "video/mp4")},
                timeout=60,
            )
        return r.status_code == 200
    except Exception as e:
        print(f"[notifier] sendVideo failed: {e}")
        return False


def send_photo(image_path: str, caption: str = "") -> bool:
    """Send a cover image to Telegram."""
    if not _BOT_TOKEN() or not _CHAT_ID():
        return False
    path = Path(image_path)
    if not path.exists():
        return False
    try:
        with open(path, "rb") as f:
            r = httpx.post(
                _url("sendPhoto"),
                data={"chat_id": _CHAT_ID(), "caption": caption[:1024], "parse_mode": "Markdown"},
                files={"photo": (path.name, f, "image/jpeg")},
                timeout=30,
            )
        return r.status_code == 200
    except Exception as e:
        print(f"[notifier] sendPhoto failed: {e}")
        return False


def notify_campaign(result: Any, copy: dict[str, Any]) -> None:
    """Send full campaign result: cover + video + copy summary."""
    brand = result.brand
    url = result.url

    # Header message
    header = (
        f"*New Ad Generated: {brand}*\n"
        f"`{url}`\n"
    )

    # TikTok copy
    tt = copy.get("tiktok", {}).get("data", {})
    ig = copy.get("instagram", {}).get("data", {})

    if tt:
        header += f"\n*TikTok*\n_{tt.get('title', '')}_\n{tt.get('body', '')[:100]}...\n"
        header += " ".join(f"#{h}" for h in tt.get("hashtags", [])[:5])

    # Send cover image with copy
    cover = Path(result.output_dir) / "cover_tiktok.jpg"
    if cover.exists():
        send_photo(str(cover), caption=header)
    else:
        send_text(header)

    # Send video
    if result.video_path and Path(result.video_path).exists():
        video_caption = ""
        if ig:
            video_caption = f"*Instagram*\n_{ig.get('title', '')}_"
        send_video(result.video_path, caption=video_caption)


def notify_daily_summary(results: list[Any], date_str: str) -> None:
    """Send end-of-day summary."""
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    lines = [
        f"*Daily Adreel Report — {date_str}*",
        f"✅ Generated: {len(ok)}/10",
    ]
    if failed:
        lines.append(f"❌ Failed: {len(failed)}")
        for r in failed:
            lines.append(f"  • {r.brand}: {r.error[:60]}")

    lines.append("\n*Brands today:*")
    for r in ok:
        lines.append(f"  • {r.brand} ({r.url.split('/')[2]})")

    send_text("\n".join(lines))
