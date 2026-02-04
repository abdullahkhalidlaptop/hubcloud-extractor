# Vercel proxy function - forwards to Render service and handles wake/health.
import os
import urllib.parse
import asyncio
import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional

app = FastAPI()
RENDER_URL = os.getenv("RENDER_SERVICE_URL")  # set this in Vercel to your Render URL, e.g. https://your-app.onrender.com
SUGGESTED_RETRY_AFTER = int(os.getenv("RETRY_AFTER", "30"))
MAX_BLOCK_WAIT = int(os.getenv("MAX_BLOCK_WAIT", "20"))

if not RENDER_URL:
    print("Warning: RENDER_SERVICE_URL not set. Set it in Vercel environment variables.")

async def forward_request(path: str, params: dict, timeout: float = 15.0):
    url = RENDER_URL.rstrip("/") + path
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        return r

async def post_wake(timeout: float = 10.0):
    url = RENDER_URL.rstrip("/") + "/wake"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url)
        return r

@app.get("/api/hubcloud")
async def proxy_get(url: Optional[str] = Query(None, alias="url"), force: Optional[bool] = Query(False), wait: Optional[bool] = Query(False)):
    if not RENDER_URL:
        raise HTTPException(status_code=500, detail="RENDER_SERVICE_URL not configured")
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' query parameter")

    params = {"url": url}
    if force:
        params["force"] = "1"

    # try fast forward
    r = None
    try:
        r = await forward_request("/api/hubcloud", params, timeout=8.0)
    except (httpx.ConnectError, httpx.ReadTimeout):
        r = None

    if r is not None and r.status_code == 200:
        return JSONResponse(status_code=200, content=r.json())

    # ask Render to wake
    try:
        await post_wake()
    except Exception:
        pass

    # optional blocking wait (not recommended if large)
    if wait:
        total_waited = 0
        interval = 2
        while total_waited < MAX_BLOCK_WAIT:
            await asyncio.sleep(interval)
            total_waited += interval
            try:
                r2 = await forward_request("/health", {}, timeout=5.0)
                if r2.status_code == 200:
                    r3 = await forward_request("/api/hubcloud", params, timeout=30.0)
                    return JSONResponse(status_code=r3.status_code, content=r3.json())
            except Exception:
                pass
        return JSONResponse(status_code=202, content={
            "status": "waking",
            "message": f"Render is starting. Retry after {SUGGESTED_RETRY_AFTER} seconds."
        }, headers={"Retry-After": str(SUGGESTED_RETRY_AFTER)})

    return JSONResponse(status_code=202, content={
        "status": "waking",
        "message": f"Render is starting. Retry after {SUGGESTED_RETRY_AFTER} seconds."
    }, headers={"Retry-After": str(SUGGESTED_RETRY_AFTER)})


@app.get("/api/hubcloud/{path_url:path}")
async def proxy_path(path_url: str, force: Optional[bool] = Query(False), wait: Optional[bool] = Query(False)):
    url = urllib.parse.unquote(path_url)
    return await proxy_get(url=url, force=force, wait=wait)