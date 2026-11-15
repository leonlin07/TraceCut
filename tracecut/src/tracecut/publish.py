"""Generate Douyin/TikTok-style publishing metadata from a rendered VideoPlan."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracecut.providers.llm import LLMProvider
    from tracecut.schemas import VideoPlan


@dataclass
class PublishMeta:
    title: str
    hashtags: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "hashtags": self.hashtags,
            "description": self.description,
        }


def _ensure_hash(tag: str) -> str:
    t = tag.strip()
    if not t:
        return ""
    return f"#{t}" if not t.startswith("#") else t


def generate_publish_meta(
    llm: "LLMProvider",
    plan: "VideoPlan",
    topic: str,
) -> PublishMeta:
    """Ask the LLM to produce a Douyin-ready title + hashtags + short description."""
    payload = {
        "topic": topic,
        "video_title": plan.title,
        "hook_title": plan.hook_title,
        "hook": plan.hook,
        "narrations": [s.narration for s in plan.scenes],
        "instruction": (
            'Return JSON: {"title": "≤20字、有悬念、不标题党的抖音标题", '
            '"hashtags": ["#xxx", ...] 3-5 个, '
            '"description": "≤50字简介"}'
        ),
    }
    try:
        raw = llm.complete_json(
            system="你是抖音内容运营专家，写吸引点击但不夸张的标题。Return ONLY JSON.",
            user=json.dumps(payload, ensure_ascii=False),
            max_tokens=400,
        )
        title = str(raw.get("title", plan.title))[:30]
        hashtags = [
            _ensure_hash(str(h)) for h in raw.get("hashtags", [])
            if str(h).strip()
        ][:5]
        description = str(raw.get("description", ""))[:80]
    except Exception:
        title = (plan.hook_title or plan.title)[:20]
        hashtags = [f"#{topic[:10]}"]
        description = ""
    return PublishMeta(title=title, hashtags=hashtags, description=description)


def write_publish_json(
    meta: PublishMeta,
    video_path: Path,
    cover_path: Path,
    output: Path,
) -> Path:
    """Write a publish.json next to the final video."""
    data = {
        "title": meta.title,
        "hashtags": meta.hashtags,
        "description": meta.description,
        "video": str(video_path),
        "cover": str(cover_path) if cover_path.exists() else "",
    }
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
