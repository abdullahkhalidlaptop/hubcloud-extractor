#!/usr/bin/env python3
"""
Railway Playwright service (run on Railway). Exposes:
 - GET /api/hubcloud?url=...
 - GET /health        -> 200 if Playwright/browser initialized
 - POST /wake         -> triggers lazy Playwright start in background (returns 202)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import urllib.parse
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, Browser, Playwright

app = FastAPI(title="Railway Playwright Hubcloud Service")

# editable timeouts
HEAD_TIMEOUT = 3
META_TIMEOUT = 4
LINKS_TIMEOUT = 4
GAMERXYT_REQ_TIMEOUT = 4

PLAYWRIGHT_NAV_TIMEOUT = 10000
PLAYWRIGHT_SELECTOR_TIMEOUT = 3000
SHORT_WAIT_MS = 300
LONG_WAIT_MS = 500

CACHE_FILE = "links_metadata.json"

# Playwright lazy state
_playwright_state: Dict[str, Optional[object]] = {"started": False, "playwright": None, "browser": None}


# -- utilities --
def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_cache() -> Dict[str, dict]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache: Dict[str, dict]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def parse_size_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r'([\d\.,]+)\s*(GB|MB|KB|B)', text, flags=re.IGNORECASE)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    unit = m.group(2).upper()
    try:
        val = float(num)
    except Exception:
        return None
    if unit == "B":
        return int(val)
    if unit == "KB":
        return int(val * 1024)
    if unit == "MB":
        return int(val * 1024 * 1024)
    if unit == "GB":
        return int(val * 1024 * 1024 * 1024)
    return None


def normalize_telegram(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    m = re.search(r'(?:/foo/|/re2/|[?&]r=|id=)([A-Za-z0-9+/=]+)', href)
    if m:
        encoded = urllib.parse.unquote(m.group(1))
        while len(encoded) % 4 != 0:
            encoded += "="
        try:
            decoded = base64.b64decode(encoded).decode("utf-8", errors="ignore")
            return decoded
        except Exception:
            return href
    return href


# -- network helpers (requests-based) --
def head_request(url: str, timeout: int = HEAD_TIMEOUT) -> Dict[str, Optional[str]]:
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        return {
            "content_type": resp.headers.get("Content-Type"),
            "content_length": resp.headers.get("Content-Length"),
            "status_code": str(resp.status_code),
        }
    except Exception:
        return {"content_type": None, "content_length": None, "status_code": None}


def extract_metadata(url: str, timeout: int = META_TIMEOUT) -> Dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HubcloudProbe/1.0)"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    def safe_select_text(sel: str):
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else None
    title = safe_select_text(".card-header") or safe_select_text("title")
    file_size = safe_select_text("li:nth-child(1) i")
    file_type = safe_select_text("li:nth-child(2) i")
    return {"title": title or "", "file_size": file_size or "", "file_type": file_type or ""}


def extract_links_requests(url: str, timeout: int = LINKS_TIMEOUT) -> Dict[str, str]:
    links: Dict[str, str] = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HubcloudProbe/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        return links
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", class_="btn"):
        text = a.get_text(strip=True)
        href = a.get("href") or ""
        if not href:
            continue
        if "FSLv2" in text:
            links["FSLv2"] = href
        elif "FSL Server" in text or (("FSL" in text) and ("FSLv2" not in text)):
            links["FSL"] = href
        elif "10Gbps" in text:
            links["10Gbps"] = href
        elif "PixelServer" in text:
            m = re.search(r'/u/([A-Za-z0-9]+)', href)
            if m:
                links["PixelServer"] = f"https://pixeldrain.com/api/file/{m.group(1)}?download"
            else:
                links["PixelServer"] = href
        elif "Telegram" in text:
            if "ampproject.org" in href:
                href = re.sub(r'https://.*?ampproject\.org/c/s/', 'https://', href)
            m = re.search(r'(https://t\.me/[^\s"\']+)', href)
            if m:
                links["Telegram"] = m.group(1)
            else:
                links["Telegram"] = href
    return links


def get_gamerxyt_requests(start_url: str, timeout: int = GAMERXYT_REQ_TIMEOUT) -> Optional[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HubcloudProbe/1.0)"}
    try:
        resp = requests.get(start_url, headers=headers, timeout=timeout, allow_redirects=True)
        final = resp.url
        if "gamerxyt.com/?r=" in final:
            return final
        m = re.search(r'https://gamerxyt\.com/\?r=[A-Za-z0-9+/=]+', resp.text)
        if m:
            return m.group(0)
    except Exception:
        pass
    return None


# -- Playwright lazy-start helpers --
async def ensure_playwright_started() -> None:
    """Start Playwright/browser only once. Idempotent."""
    if _playwright_state.get("started"):
        return
    log("Starting Playwright (this may take a few seconds)...")
    p: Playwright = await async_playwright().start()
    browser: Browser = await p.chromium.launch(headless=True)
    _playwright_state["playwright"] = p
    _playwright_state["browser"] = browser
    _playwright_state["started"] = True
    log("Playwright started.")


async def extract_buttons_with_browser(start_url: str) -> Dict[str, str]:
    """Use playwright to extract buttons/links when requests-only fails."""
    links: Dict[str, str] = {}
    await ensure_playwright_started()
    browser: Browser = _playwright_state.get("browser")
    page = await browser.new_page()
    try:
        await page.goto(start_url, timeout=PLAYWRIGHT_NAV_TIMEOUT)
        await page.wait_for_timeout(SHORT_WAIT_MS)
        try:
            await page.wait_for_selector("a.btn", timeout=PLAYWRIGHT_SELECTOR_TIMEOUT)
        except Exception:
            return links
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        for b in soup.find_all("a", class_="btn"):
            text = b.get_text(strip=True)
            href = b.get("href")
            if not href:
                continue
            if "FSLv2" in text:
                links["FSLv2"] = href
            elif "FSL Server" in text:
                links["FSL"] = href
            elif "10Gbps" in text:
                page2 = await browser.new_page()
                try:
                    await page2.goto(href, timeout=PLAYWRIGHT_NAV_TIMEOUT)
                    await page2.wait_for_timeout(LONG_WAIT_MS)
                    try:
                        await page2.wait_for_selector("a#vd", timeout=PLAYWRIGHT_SELECTOR_TIMEOUT)
                        vd_btn = await page2.query_selector("a#vd")
                        final_url = await vd_btn.get_attribute("href") if vd_btn else href
                        links["10Gbps"] = final_url
                    except Exception:
                        links["10Gbps"] = href
                finally:
                    await page2.close()
            elif "PixelServer" in text:
                m = re.search(r'/u/([A-Za-z0-9]+)', href)
                if m:
                    links["PixelServer"] = f"https://pixeldrain.com/api/file/{m.group(1)}?download"
                else:
                    links["PixelServer"] = href
            elif "Telegram" in text:
                if "ampproject.org" in href:
                    href = re.sub(r'https://.*?ampproject\.org/c/s/', 'https://', href)
                if not href.startswith("https://t.me/"):
                    m = re.search(r'(https://t\.me/[^\s]+)', href)
                    if m:
                        href = m.group(1)
                links["Telegram"] = href
    finally:
        await page.close()
    return links


async def get_gamerxyt_with_browser(start_url: str) -> Optional[str]:
    await ensure_playwright_started()
    browser: Browser = _playwright_state.get("browser")
    page = await browser.new_page()
    try:
        if "ampproject.org" in start_url:
            start_url = re.sub(r'https://.*?ampproject\.org/c/s/', 'https://', start_url)
        await page.goto(start_url, timeout=PLAYWRIGHT_NAV_TIMEOUT)
        await page.wait_for_load_state("networkidle")
        current = page.url
        if "gamerxyt.com/?r=" in current:
            return current
        html = await page.content()
        m = re.search(r'https://gamerxyt\.com/\?r=[A-Za-z0-9+/=]+', html)
        if m:
            return m.group(0)
    except Exception:
        pass
    finally:
        await page.close()
    return None


# -- core processing (requests-first, playwright fallback) --
async def process_single_url(url: str, force_refresh: bool = False) -> Dict[str, dict]:
    url = url.strip()
    if not url:
        raise ValueError("Empty URL")

    cache = load_cache()
    if not force_refresh and url in cache:
        return {url: cache[url]}

    # run HEAD/metadata/links concurrently (requests-only)
    head_task = asyncio.to_thread(head_request, url, HEAD_TIMEOUT)
    meta_task = asyncio.to_thread(extract_metadata, url, META_TIMEOUT)
    links_task = asyncio.to_thread(extract_links_requests, url, LINKS_TIMEOUT)
    head, extracted, links = await asyncio.gather(head_task, meta_task, links_task)

    # if requests didn't find links, fallback to Playwright
    if not links:
        try:
            pl_links = await extract_buttons_with_browser(url)
            if pl_links:
                links = pl_links
                log(f"Playwright fallback found links for {url}")
        except Exception as e:
            log(f"Playwright fallback error: {e}")

    # resolve amp/gamerxyt links
    resolved_list: List[str] = []
    for label, link_url in list(links.items()):
        if not link_url:
            continue
        if any(x in link_url for x in ("ampproject.org", "bloggingvector.shop/foo/", "gamerxyt.com")):
            gamerxyt = await asyncio.to_thread(get_gamerxyt_requests, link_url, GAMERXYT_REQ_TIMEOUT)
            if not gamerxyt and _playwright_state.get("started"):
                try:
                    gamerxyt = await get_gamerxyt_with_browser(link_url)
                except Exception:
                    gamerxyt = None
            hubcloud = normalize_telegram(gamerxyt)
            if hubcloud:
                resolved_list.append(hubcloud)

    title = extracted.get("title", "") if isinstance(extracted, dict) else ""
    fs_text = extracted.get("file_size", "") if isinstance(extracted, dict) else ""
    size_bytes = parse_size_from_text(fs_text) if fs_text else None

    content_type = head.get("content_type") or (extracted.get("file_type") if isinstance(extracted, dict) else None)
    if head.get("content_length"):
        content_length = head.get("content_length")
    elif size_bytes is not None:
        content_length = str(size_bytes)
    else:
        content_length = None

    meta = {"url": url, "title": title, "content_type": content_type, "content_length": content_length}
    entry = {"meta": meta, "links": links or {}, "resolved": resolved_list}
    cache[url] = entry
    save_cache(cache)
    return {url: entry}


# -- endpoints: health / wake / api/hubcloud --
@app.get("/health")
async def health():
    """Return 200 if Playwright/browser started, else 503."""
    if _playwright_state.get("started"):
        return JSONResponse(status_code=200, content={"status": "ready"})
    return JSONResponse(status_code=503, content={"status": "sleeping"})


@app.post("/wake")
async def wake_background():
    """Trigger Playwright lazy-start in the background and return quickly."""
    asyncio.create_task(ensure_playwright_started())
    return JSONResponse(status_code=202, content={"status": "waking"})


@app.get("/api/hubcloud")
async def api_hubcloud(url: Optional[str] = Query(None, alias="url"), force: Optional[bool] = Query(False)):
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' query parameter")
    try:
        result = await process_single_url(url, force_refresh=bool(force))
        return JSONResponse(content=result)
    except Exception as e:
        if not _playwright_state.get("started"):
            asyncio.create_task(ensure_playwright_started())
            return JSONResponse(status_code=202, content={
                "status": "waking",
                "message": "Railway instance is waking. Retry the request after ~30s."
            })
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hubcloud/{path_url:path}")
async def api_hubcloud_path(path_url: str, force: Optional[bool] = Query(False)):
    url = urllib.parse.unquote(path_url)
    return await api_hubcloud(url=url, force=force)
