"""web/scrape_product.py — Fetch a product URL and extract info via Gemini."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _extract_page_content(html: str, url: str) -> dict[str, str]:
    """Use BeautifulSoup to pull key signals from the page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    def meta(name: str = "", prop: str = "") -> str:
        tag = (
            soup.find("meta", attrs={"property": prop}) if prop
            else soup.find("meta", attrs={"name": name})
        )
        return (tag.get("content", "") if tag else "").strip()

    # Schema.org JSON-LD
    schema_text = ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type", "")
                if "Product" in t or "ItemPage" in t:
                    schema_text = json.dumps(item, ensure_ascii=False)[:2000]
                    break
        except Exception:
            pass

    title = (
        meta(prop="og:title")
        or meta(name="twitter:title")
        or (soup.title.string.strip() if soup.title else "")
    )
    description = (
        meta(prop="og:description")
        or meta(name="description")
        or meta(name="twitter:description")
    )
    image_url = (
        meta(prop="og:image")
        or meta(name="twitter:image")
        or meta(prop="og:image:url")
    )

    # Grab visible body text — strip scripts/styles, take first ~3000 chars
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    body_text = re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()[:3000]

    return {
        "url": url,
        "title": title[:200],
        "description": description[:500],
        "image_url": image_url,
        "schema": schema_text,
        "body_text": body_text,
    }


def _gemini_extract(content: dict[str, str], gemini_client: Any) -> dict[str, Any]:
    """Ask Gemini to turn scraped content into a structured video brief."""
    from google.genai import types

    prompt = f"""You are an expert ad creative director. Analyze this product page and output a JSON object.

Product URL: {content['url']}
Page title: {content['title']}
Meta description: {content['description']}
Schema data: {content['schema']}
Page text (excerpt): {content['body_text'][:2000]}

Output ONLY valid JSON (no markdown):
{{
  "brand_name": "<brand or company name only, not product name>",
  "logo_url": "<absolute URL of brand logo or favicon if visible on page, else empty string>",
  "product_name": "<brand + product name, concise>",
  "product_category": "<e.g. skincare, food & beverage, electronics, fashion>",
  "key_features": ["<feature 1>", "<feature 2>", "<feature 3>"],
  "target_audience": "<who buys this — age, lifestyle, values>",
  "emotional_hook": "<the core emotional reason someone buys this, not a feature>",
  "style_tone": ["<one of: fresh, premium, playful, bold, serene, luxurious, energetic>"],
  "brief": "<a 2-3 sentence video brief for a TikTok/Reels ad. Focus on the emotional story, not specs.>",
  "language": "<en or zh based on the page language>",
  "variant_image_urls": ["<full URL of color/variant product image 1>", "<url 2>"]
}}

For variant_image_urls: look in the schema data and page text for multiple product images representing
different colors or variants of the same product. Return up to 6 full image URLs.
If only one color exists, return an empty list [].
Only include actual product photo URLs (jpg/png/webp), not swatches or icons."""

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=1024,
                temperature=0.4,
            ),
        )
        text = response.text.strip()
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        # Fallback: use raw title + description as brief
        return {
            "product_name": content["title"],
            "product_category": "product",
            "key_features": [],
            "target_audience": "general audience",
            "emotional_hook": content["description"],
            "style_tone": ["fresh"],
            "brief": f"{content['title']}. {content['description']}",
            "language": "en",
        }


def _dominant_color_from_image(image_path: str | None) -> str:
    """Extract the most visually prominent non-white/non-black color from a product image."""
    if not image_path:
        return "#333333"
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        img.thumbnail((100, 100))
        pixels = list(img.getdata())
        # Filter out near-white and near-black pixels
        filtered = [
            p for p in pixels
            if not (p[0] > 220 and p[1] > 220 and p[2] > 220)  # not white
            and not (p[0] < 35 and p[1] < 35 and p[2] < 35)    # not black
        ]
        if not filtered:
            return "#333333"
        # Average the remaining pixels
        r = sum(p[0] for p in filtered) // len(filtered)
        g = sum(p[1] for p in filtered) // len(filtered)
        b = sum(p[2] for p in filtered) // len(filtered)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#333333"


def _download_image(image_url: str, dest_dir: Path) -> str | None:
    """Download the product image and return local path."""
    if not image_url:
        return None
    try:
        import httpx
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = image_url.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "webp"):
            ext = "jpg"
        dest = dest_dir / f"product_{uuid.uuid4().hex[:8]}.{ext}"
        with httpx.Client(timeout=20, follow_redirects=True, headers=_HEADERS) as client:
            resp = client.get(image_url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return str(dest)
    except Exception:
        return None


async def _screenshot_extract(url: str, data_dir: Path, gemini_client: Any) -> dict[str, Any]:
    """Fallback: use Playwright to screenshot the page, then Gemini Vision to extract info."""
    from google.genai import types as gtypes
    import base64

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "Page blocked scraping and Playwright is not installed"}

    screenshot_path = data_dir / "uploads" / f"screenshot_{uuid.uuid4().hex[:8]}.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)  # let JS render
            await page.screenshot(path=str(screenshot_path), full_page=False)
        finally:
            await browser.close()

    # Send screenshot to Gemini Vision
    img_bytes = screenshot_path.read_bytes()
    b64 = base64.b64encode(img_bytes).decode()

    prompt = f"""You are an expert ad creative director. Look at this product page screenshot.
URL: {url}

Extract product information and output ONLY valid JSON (no markdown):
{{
  "product_name": "<brand + product name>",
  "product_category": "<category>",
  "key_features": ["<feature 1>", "<feature 2>", "<feature 3>"],
  "target_audience": "<who buys this>",
  "emotional_hook": "<core emotional reason to buy>",
  "style_tone": ["<fresh|premium|playful|bold|serene|luxurious|energetic>"],
  "brief": "<2-3 sentence TikTok/Reels ad brief focusing on emotional story>",
  "language": "<en or zh>"
}}"""

    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[
            {"parts": [
                {"inline_data": {"mime_type": "image/png", "data": b64}},
                {"text": prompt},
            ]}
        ],
        config=gtypes.GenerateContentConfig(max_output_tokens=1024, temperature=0.4),
    )
    text = response.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    extracted = json.loads(text)

    return {
        **extracted,
        "image_path": str(screenshot_path),
        "image_url": "",
        "_from_screenshot": True,
    }


async def _playwright_get_html(url: str) -> str | None:
    """Use Playwright headless browser to get fully-rendered HTML."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(2000)  # let JS render
                return await page.content()
            finally:
                await browser.close()
    except Exception:
        return None


async def scrape_product(url: str, data_dir: Path, gemini_client: Any) -> dict[str, Any]:
    """Main entry: fetch URL → extract → Gemini brief → download image.

    Strategy:
    1. Fast httpx fetch → BeautifulSoup parse
    2. If blocked/empty → Playwright headless browser → BeautifulSoup parse
    3. If still empty → Playwright screenshot → Gemini Vision (last resort)
    """
    import httpx

    # 1. Try fast httpx fetch
    html = None
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception:
        pass

    # 2. Parse; if empty/JS-rendered, upgrade to Playwright HTML fetch
    content = _extract_page_content(html, url) if html else {"title": "", "body_text": "", "description": "", "image_url": "", "schema": "", "url": url}

    if not content["title"] and not content["body_text"]:
        # Try Playwright to get fully-rendered HTML
        pw_html = await _playwright_get_html(url)
        if pw_html:
            content = _extract_page_content(pw_html, url)

    if not content["title"] and not content["body_text"]:
        # Last resort: screenshot → Gemini Vision
        if gemini_client:
            try:
                return await _screenshot_extract(url, data_dir, gemini_client)
            except Exception as e:
                return {"error": f"Could not extract product info: {e}"}
        return {"error": "Could not read this page. Please describe your product manually."}

    # 3. Gemini extraction
    extracted = _gemini_extract(content, gemini_client) if gemini_client else {
        "product_name": content["title"],
        "brief": f"{content['title']}. {content['description']}",
        "style_tone": ["fresh"],
        "language": "en",
        "key_features": [],
        "target_audience": "",
        "emotional_hook": "",
        "product_category": "",
    }

    # 4. Download main product image
    img_dir = data_dir / "uploads"
    image_path = _download_image(content["image_url"], img_dir)

    # 5. Download variant images (for color-variant outro)
    variant_urls = extracted.pop("variant_image_urls", []) or []
    variant_paths: list[str] = []
    for vurl in variant_urls[:6]:  # cap at 6 variants
        if vurl and vurl != content["image_url"]:
            vpath = _download_image(vurl, img_dir)
            if vpath:
                variant_paths.append(vpath)

    # Extract dominant color from product image
    brand_primary_color = _dominant_color_from_image(image_path) if image_path else "#333333"

    # Try to download logo (favicon fallback)
    logo_path = None
    logo_url = extracted.pop("logo_url", "") or ""
    brand_name = extracted.pop("brand_name", "") or ""
    if not logo_url:
        # Try favicon from domain
        from urllib.parse import urlparse
        parsed = urlparse(url)
        logo_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
    if logo_url:
        logo_path = _download_image(logo_url, img_dir / "logos")

    # Build brand_info dict
    brand_info = {
        "brand_name": brand_name or (extracted.get("product_name", "").split()[0] if extracted.get("product_name") else ""),
        "primary_color": brand_primary_color,
        "logo_path": logo_path or "",
        "logo_url": logo_url,
    }

    return {
        **extracted,
        "image_path": image_path,
        "image_url": content["image_url"],
        "variant_image_paths": variant_paths,
        "brand_info": brand_info,
    }
