"""web/scrape_product.py — Fetch a product URL and extract info via Gemini."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any


_RETAILER_DOMAINS = {
    "sephora", "amazon", "walmart", "target", "ulta", "nordstrom",
    "macys", "bestbuy", "ebay", "etsy", "costco", "zappos", "overstock",
}


def _brand_name_from_domain(url: str) -> str:
    """Derive brand name from URL domain — reliable for DTC brands."""
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().replace("www.", "").split(".")[0]
    domain = re.sub(r"\d+$", "", domain).strip()   # strip trailing digits: stanley1913→stanley
    return domain.title() if domain else ""


def _is_retailer_url(url: str) -> bool:
    from urllib.parse import urlparse
    base = urlparse(url).netloc.lower().replace("www.", "").split(".")[0]
    return base in _RETAILER_DOMAINS


def _clean_brand_name(raw: str, url: str) -> str:
    """Post-process Gemini's brand_name to return just the company name."""
    if _is_retailer_url(url):
        # For retailer pages: Gemini must identify the actual product brand
        # Extract just the brand: "The X ..." → "The X", else first word
        if not raw:
            return ""  # can't infer from domain for retailers
        words = raw.split()
        if words and words[0].lower() == "the" and len(words) >= 2:
            return f"The {words[1]}"
        return words[0] if words else raw
    # For DTC brand sites, domain is the canonical brand name (more reliable)
    domain_brand = _brand_name_from_domain(url)
    return domain_brand if domain_brand else (raw.split()[0] if raw else "")


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

    # Schema.org JSON-LD — also extract image URL from Product schema
    schema_text = ""
    schema_image_url = ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type", "")
                if "Product" in t or "ItemPage" in t:
                    schema_text = json.dumps(item, ensure_ascii=False)[:2000]
                    # image field can be a string, list, or dict
                    img = item.get("image", "")
                    if isinstance(img, list) and img:
                        img = img[0]
                    if isinstance(img, dict):
                        img = img.get("url", "")
                    if isinstance(img, str) and img.startswith("http"):
                        schema_image_url = img.split("?")[0]  # strip query params
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
        or schema_image_url  # JSON-LD Product schema image as final fallback
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
  "brand_name": "<BRAND name ONLY — single company name, NO model/product details. E.g. 'Nike' not 'Nike Air Force 1', 'Allbirds' not 'Allbirds Wool Runner', 'Gymshark' not 'Gymshark Vital Seamless'. If retailer page (Sephora/Amazon/Walmart), use the actual product brand.>",
  "logo_url": "<absolute URL of brand logo or favicon if visible on page, else empty string>",
  "product_name": "<brand + specific product name, e.g. 'Nike Air Force 1 07', 'Allbirds Wool Runner' — should be longer and more specific than brand_name>",
  "product_category": "<e.g. skincare, food & beverage, electronics, fashion>",
  "key_features": ["<feature 1>", "<feature 2>", "<feature 3>"],
  "target_audience": "<specific: age range, gender if relevant, lifestyle, values — e.g. 'Women 25-40, fitness-focused, value sustainability'>",
  "emotional_hook": "<the core emotional reason someone buys this, not a feature>",
  "style_tone": "<exactly ONE word from: fresh, premium, playful, bold, serene, luxurious, energetic — pick the single best fit>",
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
        data = json.loads(text)
        # Normalise style_tone to a single-element list
        tone = data.get("style_tone", "fresh")
        if isinstance(tone, list):
            tone = tone[0] if tone else "fresh"
        data["style_tone"] = [tone]
        return data
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


def _google_image_search(query: str) -> str | None:
    """Search Google Custom Search for a product image, return the first image URL."""
    api_key = os.getenv("GOOGLE_API_KEY")
    cx = os.getenv("GOOGLE_CSE_ID")
    if not api_key or not cx:
        return None
    try:
        import httpx
        resp = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": api_key, "cx": cx, "q": query, "searchType": "image", "num": 1},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            return items[0].get("link")
    except Exception:
        pass
    return None


def _extract_images_from_markdown(text: str) -> list[str]:
    """Pull image URLs out of Jina markdown (![alt](url) syntax)."""
    candidates = re.findall(r'!\[.*?\]\((https?://[^\s)]+)\)', text)
    # Keep only plausible product images; drop icons / trackers / tiny pixels
    skip = ("icon", "logo", "favicon", "sprite", "1x1", "pixel", "badge",
            "avatar", "svg", ".gif", "/nav/", "nav_", "navigation",
            "banner", "marketing_tile", "header", "footer", "menu")
    return [
        u for u in candidates
        if any(u.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))
        and not any(s in u.lower() for s in skip)
    ][:10]


def _gemini_pick_product_image(
    candidates: list[str], product_name: str, gemini_client: Any
) -> str | None:
    """Ask Gemini to choose the best product-photo URL from a list of candidates."""
    from google.genai import types as gtypes

    if not candidates or not gemini_client:
        return None
    if len(candidates) == 1:
        return candidates[0]
    try:
        numbered = "\n".join(f"{i+1}. {u}" for i, u in enumerate(candidates))
        prompt = (
            f"Product: {product_name}\n\n"
            f"These image URLs were found on the product page:\n{numbered}\n\n"
            "Which single URL is most likely the main product photo (not a logo, banner, or UI element)? "
            "Reply with ONLY the URL, nothing else."
        )
        resp = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=gtypes.GenerateContentConfig(max_output_tokens=200, temperature=0.0),
        )
        url = resp.text.strip().strip('"').strip("'")
        if url.startswith("http"):
            return url
    except Exception:
        pass
    return candidates[0]  # fall back to first candidate


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

    # Normalize style_tone and brand_name
    raw_tone = extracted.get("style_tone", "fresh")
    if isinstance(raw_tone, list):
        raw_tone = raw_tone[0] if raw_tone else "fresh"
    extracted["style_tone"] = [raw_tone]
    brand_name = _clean_brand_name(extracted.get("brand_name", "") or "", url)
    if not brand_name:
        brand_name = _clean_brand_name(extracted.get("product_name", ""), url)
    extracted["brand_name"] = brand_name

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


async def _jina_fetch(url: str) -> str | None:
    """Fetch via Jina AI Reader (r.jina.ai) which handles JS-rendered pages for free."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                f"https://r.jina.ai/{url}",
                headers={"Accept": "text/markdown", "X-No-Cache": "true"},
            )
            resp.raise_for_status()
            text = resp.text.strip()
            if len(text) > 300:
                return text
    except Exception:
        pass
    return None


async def _brand_intelligence_fallback(url: str, data_dir: Path, gemini_client: Any = None) -> dict[str, Any]:
    """When scraping fails, use Gemini's knowledge + Clearbit logo to return brand info."""
    from google.genai import types as gtypes
    from urllib.parse import urlparse

    domain = urlparse(url).netloc.lstrip("www.")
    brand_name_guess = domain.split(".")[0].title()

    # Ask Gemini what it knows about this brand and specific product
    brand_raw: dict = {}
    if gemini_client:
        try:
            prompt = f"""A user wants a video ad for a product. The exact product page URL is:
{url}

The page couldn't be scraped. Using your knowledge of this brand and the product details
visible in the URL path, infer as much as possible about the specific product.

Return ONLY valid JSON, no markdown:
{{
  "brand_name": "<BRAND name ONLY — single company name, NO model/product details. E.g. 'Nike' not 'Nike Air Force 1', 'CeraVe' not 'CeraVe Moisturizing Cream'>",
  "product_name": "<brand + specific product name inferred from URL path, e.g. 'Nike Air Force 1 07', 'CeraVe Moisturizing Cream' — more specific than brand_name>",
  "brand_description": "<one sentence: what they sell and who it's for>",
  "product_category": "<e.g. fashion, food & beverage, beauty, electronics, activewear>",
  "key_features": ["<feature 1>", "<feature 2>", "<feature 3>"],
  "target_audience": "<specific: age range, gender if relevant, lifestyle, values — e.g. 'Women 25-40, fitness-focused, value sustainability'>",
  "style_tone": "<exactly ONE word from: fresh, premium, playful, bold, serene, luxurious, energetic>",
  "primary_color_hex": "<brand's signature color as hex>",
  "brief": "<2-3 sentence TikTok/Reels video brief, emotional story not specs>",
  "known_brand": <true if you know this brand, false if unfamiliar>
}}"""
            resp = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=gtypes.GenerateContentConfig(max_output_tokens=500, temperature=0.4),
            )
            text = resp.text.strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            brand_raw = json.loads(text)
        except Exception as e:
            print(f"[scrape] Brand intelligence (Gemini) failed: {e}")

    brand_name = _clean_brand_name(brand_raw.get("brand_name") or brand_name_guess, url)
    product_name = brand_raw.get("product_name") or brand_name
    img_dir = data_dir / "uploads" / "logos"

    # Try product image first via CSE, then fall back to logo sources
    image_url = ""
    image_path = None

    # 1. CSE: search for the specific product image
    cse_product = _google_image_search(f"{product_name} product photo")
    if cse_product:
        image_path = _download_image(cse_product, img_dir)
        if image_path:
            image_url = cse_product

    # 2. Logo: Clearbit → CSE logo → favicon
    logo_url = ""
    logo_path = None
    clearbit = f"https://logo.clearbit.com/{domain}"
    logo_path = _download_image(clearbit, img_dir)
    if logo_path:
        logo_url = clearbit

    if not logo_path:
        cse_logo = _google_image_search(f"{brand_name} logo transparent")
        if cse_logo:
            logo_path = _download_image(cse_logo, img_dir)
            logo_url = cse_logo or ""

    if not logo_path:
        favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
        logo_path = _download_image(favicon, img_dir)
        logo_url = favicon if logo_path else ""

    # Use product image as main image; fall back to logo
    final_image_path = image_path or logo_path or ""
    final_image_url = image_url or logo_url

    primary_color = brand_raw.get("primary_color_hex", "#333333")
    brief = brand_raw.get("brief", f"Create a compelling video ad for {product_name}.")

    # Normalise style_tone to a list with exactly one element
    raw_tone = brand_raw.get("style_tone", "fresh")
    if isinstance(raw_tone, list):
        raw_tone = raw_tone[0] if raw_tone else "fresh"
    style_tone = [raw_tone]

    return {
        "mode": "intelligence",
        "brand_name": brand_name,
        "brand_description": brand_raw.get("brand_description", f"Products from {domain}"),
        "product_name": product_name,
        "product_category": brand_raw.get("product_category", "product"),
        "key_features": brand_raw.get("key_features", []),
        "target_audience": brand_raw.get("target_audience", ""),
        "style_tone": style_tone,
        "brief": brief,
        "primary_color": primary_color,
        "logo_url": logo_url,
        "logo_path": logo_path or "",
        "known_brand": brand_raw.get("known_brand", False),
        "emotional_hook": brief,
        "image_path": final_image_path,
        "image_url": final_image_url,
        "variant_image_paths": [],
        "brand_info": {
            "brand_name": brand_name,
            "primary_color": primary_color,
            "logo_path": logo_path or "",
            "logo_url": logo_url,
        },
    }


_GARBAGE_TITLES = [
    "access denied", "403 forbidden", "404", "not found", "error",
    "just a moment", "attention required", "pardon our interruption",
    "hang tight", "routing to checkout", "captcha", "enable javascript",
    "please wait", "checking your browser",
]


def _is_garbage_content(content: dict) -> bool:
    """Return True if scraped content looks like a blocked/error page."""
    title = content.get("title", "").lower()
    return any(m in title for m in _GARBAGE_TITLES)


async def scrape_product(url: str, data_dir: Path, gemini_client: Any) -> dict[str, Any]:
    """Main entry: fetch URL → extract → Gemini brief → download image.

    Strategy:
    1. Fast httpx fetch → BeautifulSoup parse
    2. Jina AI Reader (handles JS rendering, most anti-bot) → Gemini extract
    3. Playwright headless browser → BeautifulSoup parse
    4. Playwright screenshot → Gemini Vision
    5. Brand Intelligence fallback (LLM knowledge + Clearbit logo)
    """
    import httpx

    # 1. Try fast httpx fetch; retry without www. on DNS/connection failure
    html = None
    effective_url = url
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
        if "://www." in url:
            # www. subdomain may not exist (e.g. www.dinq.me has no DNS record).
            # Strip it and use the apex domain for all subsequent pipeline steps.
            effective_url = url.replace("://www.", "://", 1)
            try:
                async with httpx.AsyncClient(
                    timeout=15,
                    follow_redirects=True,
                    headers=_HEADERS,
                ) as client:
                    resp = await client.get(effective_url)
                    resp.raise_for_status()
                    html = resp.text
            except Exception:
                pass  # apex also failed; later steps (Jina/Brand Intel) will use effective_url

    # Parse HTML; check if useful content extracted
    content = _extract_page_content(html, effective_url) if html else {
        "title": "", "body_text": "", "description": "",
        "image_url": "", "schema": "", "url": effective_url,
    }

    # 2. If empty OR garbage (blocked/404/anti-bot), try Jina AI Reader
    need_jina = (not content["title"] and not content["body_text"]) or _is_garbage_content(content)
    if need_jina:
        jina_text = await _jina_fetch(effective_url)
        if jina_text:
            first_line = jina_text.split("\n")[0].lstrip("# ").strip()
            jina_content = {
                "url": effective_url,
                "title": first_line[:200],
                "description": "",
                "image_url": "",
                "schema": "",
                "body_text": jina_text[:3000],
            }
            # Only use Jina result if it's not also garbage
            if not _is_garbage_content(jina_content):
                content = jina_content
                need_jina = False  # Jina succeeded

    # 3. If still empty/garbage, try Playwright HTML fetch
    if not content["title"] and not content["body_text"] or (need_jina and _is_garbage_content(content)):
        pw_html = await _playwright_get_html(effective_url)
        if pw_html:
            pw_content = _extract_page_content(pw_html, effective_url)
            if not _is_garbage_content(pw_content):
                content = pw_content

    # 4. Last resort: Playwright screenshot → Gemini Vision
    if not content["title"] and not content["body_text"] or _is_garbage_content(content):
        if gemini_client:
            try:
                return await _screenshot_extract(effective_url, data_dir, gemini_client)
            except Exception:
                pass
        # 5. Brand Intelligence fallback — LLM knowledge + logo
        return await _brand_intelligence_fallback(effective_url, data_dir, gemini_client)

    # Gemini extraction (or Brand Intelligence if no Gemini key)
    if gemini_client:
        extracted = _gemini_extract(content, gemini_client)
    else:
        # No Gemini key — use Brand Intelligence for proper extraction
        return await _brand_intelligence_fallback(effective_url, data_dir, gemini_client)

    # Normalize style_tone to exactly one element (Gemini sometimes returns list or csv)
    raw_tone = extracted.get("style_tone", "fresh")
    if isinstance(raw_tone, list):
        raw_tone = raw_tone[0] if raw_tone else "fresh"
    if isinstance(raw_tone, str) and "," in raw_tone:
        raw_tone = raw_tone.split(",")[0].strip()
    extracted["style_tone"] = [raw_tone]

    # 4. Download main product image; fall back to Google Image Search if missing
    img_dir = data_dir / "uploads"
    image_url = content["image_url"]
    product_name = extracted.get("product_name", "") or content.get("title", "")

    # Image source A: og:image from HTML (already in content["image_url"])
    image_path = _download_image(image_url, img_dir) if image_url else None

    # Image source A fallback: Google Custom Search (requires CSE API enabled)
    if not image_path and product_name:
        cse_url = _google_image_search(product_name)
        if cse_url:
            image_path = _download_image(cse_url, img_dir)
            if image_path:
                image_url = cse_url

    # Image source B: extract from Jina markdown → Gemini picks best candidate
    if not image_path and content.get("body_text"):
        md_candidates = _extract_images_from_markdown(content["body_text"])
        if md_candidates:
            best_url = _gemini_pick_product_image(md_candidates, product_name, gemini_client)
            if best_url:
                image_path = _download_image(best_url, img_dir)
                if image_path:
                    image_url = best_url

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

    # Try to download logo — priority: Gemini-found URL → apple-touch-icon → favicon.ico
    logo_path = None
    logo_url = extracted.pop("logo_url", "") or ""
    brand_name = _clean_brand_name(extracted.pop("brand_name", "") or "", effective_url)
    if not brand_name:
        # Retailer page where Gemini left brand_name empty — derive from product_name
        brand_name = _clean_brand_name(extracted.get("product_name", ""), effective_url)
    from urllib.parse import urlparse, urljoin
    _parsed = urlparse(effective_url)
    _origin = f"{_parsed.scheme}://{_parsed.netloc}"
    if not logo_url and html:
        # Search <link> tags for high-quality icons (apple-touch-icon > icon > shortcut icon)
        try:
            from bs4 import BeautifulSoup as _BS
            _soup = _BS(html, "html.parser")
            for _rel in ("apple-touch-icon", "apple-touch-icon-precomposed", "icon", "shortcut icon"):
                _tag = _soup.find("link", rel=lambda r: r and _rel in (r if isinstance(r, list) else [r]))
                if _tag and _tag.get("href"):
                    logo_url = urljoin(_origin, _tag["href"])
                    break
        except Exception:
            pass
    if not logo_url:
        logo_url = f"{_origin}/favicon.ico"
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
        "brand_name": brand_name,
        "image_path": image_path,
        "image_url": image_url,
        "variant_image_paths": variant_paths,
        "brand_info": brand_info,
    }
