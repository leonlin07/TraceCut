"""FastAPI service exposing the TraceCut pipeline over HTTP.

Run with:  uvicorn serve.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from serve.provenance import render_provenance_html
from serve.worker import JobManager
from tracecut.config import Settings

settings = Settings.from_env()
manager = JobManager(settings)
app = FastAPI(title="TraceCut Demo", version="0.1.0")


class GenerateRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    source_urls: list[str] = Field(default_factory=list)
    duration_s: int = 30
    render: bool = True
    rubric_iters: int = 0
    run_vl_judge: bool = False
    vl_loop: int = 0
    director: bool = False


@app.post("/generate")
def generate(req: GenerateRequest) -> dict[str, str]:
    """Submit a generation job. Returns immediately with a job_id to poll."""
    job_id = manager.submit(
        req.topic,
        req.source_urls,
        duration_s=req.duration_s,
        render=req.render,
        rubric_iters=req.rubric_iters,
        run_vl_judge=req.run_vl_judge,
        vl_loop=req.vl_loop,
        director=req.director,
    )
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/cancel/{job_id}")
def cancel(job_id: str) -> dict[str, Any]:
    """Request cancellation of a running job (best-effort, stage-boundary)."""
    if not manager.cancel(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "cancelled": True}


@app.get("/result/{job_id}")
def result(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "done":
        raise HTTPException(
            status_code=409, detail=f"job not finished (status={job['status']})"
        )
    manifest = _load_manifest(job["manifest_path"])
    return {
        "job_id": job_id,
        "manifest_path": job["manifest_path"],
        "output_video": (manifest or {}).get("output_video", ""),
        "subtitle_path": (manifest or {}).get("subtitle_path", ""),
        "evaluation": (manifest or {}).get("evaluation", {}),
        "rubric": (manifest or {}).get("rubric"),
        "vl": (manifest or {}).get("vl"),
        "warnings": (manifest or {}).get("warnings", []),
        "provenance_html": render_provenance_html(manifest) if manifest else "",
        "cost": job["cost"],
        "elapsed_s": job["elapsed_s"],
    }


def _load_manifest(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
