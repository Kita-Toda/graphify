"""
Spruce My Site — Lead Generator API
FastAPI backend that wraps the lead-gen engine and streams live progress
via Server-Sent Events (SSE).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Allow importing from repo root
# Walk up from this file until we find the directory that contains tools/lead_gen/
_here = Path(__file__).resolve()
_repo = next(
    (p for p in _here.parents if (p / "tools" / "lead_gen" / "lead_gen.py").exists()),
    _here.parent.parent,
)
sys.path.insert(0, str(_repo))
from tools.lead_gen.lead_gen import (
    CATEGORIES,
    SPRUCE_SERVICES,
    Business,
    analyse_all,
    fetch_businesses,
    _service_label,
)

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Spruce My Site — Lead Generator", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory job store ────────────────────────────────────────────────────────

class Job:
    def __init__(self) -> None:
        self.id: str = str(uuid.uuid4())
        self.status: str = "pending"      # pending | running | done | error
        self.log: list[str] = []
        self.businesses: list[dict] = []
        self.summary: dict = {}
        self.error: str = ""
        self._events: asyncio.Queue = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None

    def push(self, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data})
        if self.loop:
            self.loop.call_soon_threadsafe(self._events.put_nowait, payload)

JOBS: dict[str, Job] = {}
_executor = ThreadPoolExecutor(max_workers=4)


# ── Schema ─────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    location: str = Field(..., examples=["Manchester, UK"])
    category: str = Field(..., examples=["restaurant"])
    radius:   float = Field(5.0, ge=0.5, le=50)
    limit:    int   = Field(30, ge=1, le=100)
    workers:  int   = Field(5, ge=1, le=10)
    timeout:  int   = Field(10, ge=3, le=30)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _biz_to_dict(b: Business) -> dict:
    bd = b.score_breakdown
    ts = b.tech_stack
    return {
        "name":          b.name,
        "address":       b.address,
        "phone":         b.phone,
        "website":       b.website,
        "final_url":     b.final_url,
        "score":         b.score,
        "lead_grade":    b.lead_grade,
        "error":         b.error,
        "response_time": round(b.response_time, 2),
        "lat":           b.lat,
        "lon":           b.lon,
        "score_breakdown": {
            "https":   bd.https   if bd else 0,
            "speed":   bd.speed   if bd else 0,
            "mobile":  bd.mobile  if bd else 0,
            "seo":     bd.seo     if bd else 0,
            "content": bd.content if bd else 0,
        } if bd else None,
        "tech_stack": {
            "server":    ts.server    if ts else "",
            "cms":       ts.cms       if ts else "",
            "framework": ts.framework if ts else "",
            "js_lib":    ts.js_lib    if ts else "",
            "css_fw":    ts.css_fw    if ts else "",
            "analytics": ts.analytics if ts else [],
            "ecommerce": ts.ecommerce if ts else "",
            "chat":      ts.chat      if ts else "",
            "marketing": ts.marketing if ts else "",
            "maps":      ts.maps      if ts else "",
            "payment":   ts.payment   if ts else "",
            "backend":   ts.backend   if ts else "",
            "cdn":       ts.cdn       if ts else "",
        } if ts else None,
        "missing_services": [
            {"key": k, "label": lbl, "description": desc}
            for k, lbl, desc in SPRUCE_SERVICES
            if k in b.missing_services
        ],
    }


# ── Background scan task ───────────────────────────────────────────────────────

def _run_scan(job: Job, req: ScanRequest) -> None:
    """Runs in a thread-pool worker; pushes SSE events via job.push()."""
    job.status = "running"
    try:
        # Step 1: geocode + discover
        job.push("log", f"Geocoding '{req.location}' …")
        businesses = fetch_businesses(req.location, req.category, req.radius, req.limit)
        with_site = sum(1 for b in businesses if b.website)
        job.push("log", f"Found {len(businesses)} businesses — {with_site} have websites")
        job.push("discovery", {
            "total": len(businesses),
            "with_site": with_site,
            "businesses": [_biz_to_dict(b) for b in businesses],
        })

        # Step 2: analyse websites one by one, pushing each result live
        total = len([b for b in businesses if b.website])
        done  = 0

        def on_done(b: Business) -> None:
            nonlocal done
            done += 1
            job.push("business_analysed", {
                "progress": {"done": done, "total": total},
                "business": _biz_to_dict(b),
            })

        # Patch analyse_all to get per-business callbacks
        from concurrent.futures import ThreadPoolExecutor as TPE, as_completed
        from tools.lead_gen.lead_gen import _analyse
        with TPE(max_workers=req.workers) as pool:
            futures = {
                pool.submit(_analyse, b, req.timeout): b
                for b in businesses if b.website
            }
            for fut in as_completed(futures):
                biz = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    biz.error = str(exc)
                on_done(biz)

        # Step 3: build summary
        scored   = [b for b in businesses if b.score is not None]
        avg      = int(sum(b.score for b in scored) / len(scored)) if scored else 0
        prime    = sum(1 for b in businesses
                       if not b.website or (b.score is not None and b.score <= 30))

        service_freq: dict[str, int] = {}
        for b in businesses:
            for k in b.missing_services:
                service_freq[k] = service_freq.get(k, 0) + 1

        job.summary = {
            "total":         len(businesses),
            "with_site":     with_site,
            "without_site":  len(businesses) - with_site,
            "avg_score":     avg,
            "prime_leads":   prime,
            "top_opportunities": [
                {"key": k, "label": _service_label(k), "count": v}
                for k, v in sorted(service_freq.items(), key=lambda x: -x[1])[:8]
            ],
        }
        job.businesses = [_biz_to_dict(b) for b in businesses]
        job.status = "done"
        job.push("done", {"summary": job.summary, "businesses": job.businesses})

    except Exception as exc:
        job.status = "error"
        job.error  = str(exc)
        job.push("error", {"message": str(exc)})


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/api/categories")
def list_categories() -> dict:
    return {"categories": sorted(CATEGORIES.keys())}


@app.post("/api/scan")
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks) -> dict:
    job = Job()
    job.loop = asyncio.get_event_loop()
    JOBS[job.id] = job

    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_scan, job, req)

    return {"job_id": job.id}


@app.get("/api/scan/{job_id}/stream")
async def stream_scan(job_id: str) -> StreamingResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    async def event_generator():
        # Send current state immediately if already running/done
        if job.businesses:
            for b in job.businesses:
                yield f"data: {json.dumps({'event':'business_analysed','data':{'business':b}})}\n\n"

        if job.status == "done":
            yield f"data: {json.dumps({'event':'done','data':{'summary':job.summary,'businesses':job.businesses}})}\n\n"
            return

        # Stream live events
        while True:
            try:
                payload = await asyncio.wait_for(job._events.get(), timeout=30)
                yield f"data: {payload}\n\n"
                parsed = json.loads(payload)
                if parsed.get("event") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                if job.status in ("done", "error"):
                    break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/scan/{job_id}")
def get_scan(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "job_id":     job.id,
        "status":     job.status,
        "error":      job.error,
        "summary":    job.summary,
        "businesses": job.businesses,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
