"""VL (vision-language) judge for visual relevance.

Complements the text-only rubric with an actual look at the rendered frames.
For each scene it samples one frame from the rendered video, sends it to
Qwen3-VL along with the scene's narration + visual_query, and scores:

- relevance (1-5): does the frame visually match what the narration is saying?
- misleading_visual (bool): could a viewer mistake this generic stock footage
  as *evidence* for the claim? (RQ2's out-of-context / misleading-visual signal.)

This is the first dimension that actually evaluates the PICTURE rather than the
plan text — closing the gap that left visual relevance unchecked while the rest
of the rubric scored only narration.

``rerank_candidates`` reuses the same VL model to score Pexels preview posters
BEFORE download, so the pipeline can pick the most relevant B-roll per scene
instead of the first non-duplicate hit.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tracecut.providers.llm import LLMProvider
from tracecut.schemas import Material
from tracecut.schemas import VideoPlan


@dataclass
class SceneVisualScore:
    scene_id: str
    relevance: int  # 1-5
    misleading_visual: bool
    note: str = ""


@dataclass
class VLReport:
    scores: list[SceneVisualScore] = field(default_factory=list)
    model: str = ""

    @property
    def average(self) -> float:
        if not self.scores:
            return 0.0
        return sum(s.relevance for s in self.scores) / len(self.scores)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "average": round(self.average, 3),
            "scores": [
                {
                    "scene_id": s.scene_id,
                    "relevance": s.relevance,
                    "misleading_visual": s.misleading_visual,
                    "note": s.note,
                }
                for s in self.scores
            ],
        }


_SYSTEM = (
    "You are a strict short-video visual auditor. You see ONE frame from a scene "
    "and the scene's narration + intended visual query. Score how well the frame "
    "visually matches the narration's subject (1=mismatch, 5=exact match). Also "
    "judge whether the frame is generic stock footage that a viewer might mistake "
    "as factual evidence for the claim (misleading_visual). Return ONLY JSON."
)


def _extract_frame(video: Path, time_s: float, out: Path) -> bool:
    """Extract one frame at time_s. Returns True on success."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{max(0.0, time_s):.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "3", str(out)],
        capture_output=True, timeout=60,
    )
    return result.returncode == 0 and out.exists() and out.stat().st_size > 0


def _scene_time_offsets(
    scene_durations: dict[str, float], title_offset: float
) -> dict[str, float]:
    """Map each scene to the midpoint timestamp in the rendered video.

    The render prepends a title card of `title_offset` seconds, then each scene
    lasts its actual (content-driven) duration. Frame sample at the scene's
    midpoint for a stable representative frame.
    """
    offsets: dict[str, float] = {}
    cursor = title_offset
    for scene_id, dur in scene_durations.items():
        offsets[scene_id] = cursor + dur / 2
        cursor += dur
    return offsets


def judge_visual_relevance(
    vl_provider: LLMProvider,
    plan: VideoPlan,
    video_path: Path,
    work_dir: Path,
    scene_durations: dict[str, float],
    *,
    title_offset: float = 2.6,
) -> VLReport:
    """Score each scene's frame against its narration via the VL model.

    `video_path` must be the rendered final.mp4. `scene_durations` is the actual
    per-scene duration dict returned by media.render_video. `title_offset` must
    match the title-card duration used there (2.6s). If the video is missing or a
    frame can't be extracted, that scene is skipped (score omitted rather than
    crashing the run).
    """
    report = VLReport(model=vl_provider.spec.model)
    if not video_path.exists():
        return report
    work_dir.mkdir(parents=True, exist_ok=True)
    offsets = _scene_time_offsets(scene_durations, title_offset)
    for scene in plan.scenes:
        t = offsets.get(scene.scene_id)
        if t is None:
            continue
        frame = work_dir / f"{scene.scene_id}.jpg"
        if not _extract_frame(video_path, t, frame):
            continue
        img_b64 = base64.b64encode(frame.read_bytes()).decode()
        prompt = {
            "task": "Score this frame against the scene's narration.",
            "scene_id": scene.scene_id,
            "narration": scene.narration,
            "visual_query_en": scene.visual_query_en,
            "instruction": (
                'Return JSON: {"relevance": int(1-5), "misleading_visual": bool, '
                '"note": "concise Chinese note"}. misleading_visual=true if the '
                "frame is generic stock that could be mistaken as factual evidence "
                "for the claim; false if it is clearly illustrative/non-evidential."
            ),
        }
        try:
            raw = vl_provider.complete_json_with_image(
                system=_SYSTEM,
                user=json.dumps(prompt, ensure_ascii=False),
                image_b64=img_b64,
                max_tokens=300,
            )
        except Exception:
            continue
        try:
            report.scores.append(
                SceneVisualScore(
                    scene_id=scene.scene_id,
                    relevance=int(raw.get("relevance", 3)),
                    misleading_visual=bool(raw.get("misleading_visual", False)),
                    note=str(raw.get("note", ""))[:300],
                )
            )
        except Exception:
            continue
    return report


_RERANK_SYSTEM = (
    "You are a B-roll selection judge. You see a candidate stock video's preview "
    "poster and the scene narration it would illustrate. Score how well the poster "
    "matches the narration's subject (1=mismatch, 5=exact). Return ONLY JSON."
)


def _fetch_preview_b64(url: str) -> str | None:
    """Fetch a Pexels preview poster as base64 JPEG. trust_env=False; the host is
    already constrained to images.pexels.com by the caller."""
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True, trust_env=False) as c:
            r = c.get(url)
            if r.status_code != 200 or not r.content:
                return None
            return base64.b64encode(r.content).decode()
    except httpx.HTTPError:
        return None


def rerank_candidates(
    vl_provider: LLMProvider,
    narration: str,
    candidates: list[Material],
) -> list[tuple[Material, int]]:
    """Score each Pexels candidate's preview poster against the scene narration.

    Returns (candidate, relevance) pairs sorted descending by relevance. Candidates
    whose preview can't be fetched or scored get relevance 0 (sorted last). Used to
    pick the most relevant B-roll BEFORE download, replacing the old "first
    non-duplicate" heuristic.
    """
    scored: list[tuple[Material, int]] = []
    for cand in candidates:
        if not cand.preview_image_url:
            scored.append((cand, 0))
            continue
        img_b64 = _fetch_preview_b64(cand.preview_image_url)
        if not img_b64:
            scored.append((cand, 0))
            continue
        prompt = {
            "task": "Score this stock-video poster against the narration it illustrates.",
            "narration": narration,
            "instruction": (
                'Return JSON: {"relevance": int(1-5)}. 5=poster clearly shows the '
                "narration's subject; 1=poster is unrelated or generic filler."
            ),
        }
        try:
            raw = vl_provider.complete_json_with_image(
                system=_RERANK_SYSTEM,
                user=json.dumps(prompt, ensure_ascii=False),
                image_b64=img_b64,
                max_tokens=80,
            )
            score = int(raw.get("relevance", 0))
        except Exception:
            score = 0
        scored.append((cand, max(0, min(5, score))))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
