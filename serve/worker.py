"""Async job runner for the TraceCut demo service.

Wraps the synchronous ``TraceCutPipeline.run`` in a background thread, tracks
coarse stage progress (via the pipeline's ``progress`` callback), and collects
token usage through the process-wide usage sink so the cost tracker can produce
a per-run cost report.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracecut.config import Settings
from tracecut.pipeline import TraceCutPipeline
from tracecut.providers.llm import set_usage_sink


class JobCancelled(Exception):
    """Raised inside a job's progress callback when cancellation is requested.

    It propagates out of ``pipeline.run`` at the next stage boundary, so the
    worker can mark the job as cancelled instead of failed.
    """


@dataclass
class Job:
    job_id: str
    topic: str
    status: str = "queued"  # queued / running / done / failed / cancelled
    stage: str = ""
    percent: float = 0.0
    error: str = ""
    manifest_path: str = ""
    cost: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    finished_at: float = 0.0
    elapsed_s: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)


class JobManager:
    """In-memory job registry. Fine for a single-process demo; a production
    deployment would swap this for Redis + a task queue."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        topic: str,
        source_urls: list[str],
        *,
        duration_s: int = 30,
        render: bool = True,
        rubric_iters: int = 0,
        run_vl_judge: bool = False,
        vl_loop: int = 0,
        director: bool = False,
    ) -> str:
        job = Job(job_id=uuid.uuid4().hex[:12], topic=topic, created_at=time.time())
        with self._lock:
            self._jobs[job.job_id] = job
        kwargs = dict(
            topic=topic,
            source_urls=source_urls,
            duration_s=duration_s,
            render=render,
            rubric_iters=rubric_iters,
            run_vl_judge=run_vl_judge,
            vl_loop=vl_loop,
            director=director,
        )
        threading.Thread(target=self._run, args=(job, kwargs), daemon=True).start()
        return job.job_id

    def _run(self, job: Job, kwargs: dict[str, Any]) -> None:
        job.status = "running"
        usage: list[dict] = []
        set_usage_sink(usage)
        start = time.perf_counter()
        try:
            pipeline = TraceCutPipeline(self.settings)

            def on_progress(stage: str, percent: float) -> None:
                # 取消请求通过抛异常向上传播，在 pipeline 的下一个阶段边界终止，
                # 从而跳过后续的 LLM/TTS/渲染开销。
                if job.cancel_event.is_set():
                    raise JobCancelled()
                job.stage = stage
                job.percent = percent

            manifest_path = pipeline.run(progress=on_progress, **kwargs)
            job.manifest_path = str(manifest_path)
            job.cost = self._cost_report(usage, manifest_path)
            job.status = "done"
        except JobCancelled:
            job.status = "cancelled"
            job.error = "已取消"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            set_usage_sink(None)
            job.elapsed_s = round(time.perf_counter() - start, 1)
            job.finished_at = time.time()

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of a running job. Returns True if the job exists."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.cancel_event.set()
        return True

    def _cost_report(self, usage: list[dict], manifest_path: str) -> dict[str, Any]:
        from cost.tracker import summarize

        tts_chars = 0
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            plan = manifest.get("plan", {})
            tts_chars = len(plan.get("hook", "") or "")
            for scene in plan.get("scenes", []):
                tts_chars += len(scene.get("narration", "") or "")
        except Exception:
            pass
        return summarize(usage, tts_chars=tts_chars).to_dict()

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job.job_id,
            "topic": job.topic,
            "status": job.status,
            "stage": job.stage,
            "percent": job.percent,
            "error": job.error,
            "manifest_path": job.manifest_path,
            "cost": job.cost,
            "elapsed_s": job.elapsed_s,
        }
