from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


ID_PATTERN = r"^[A-Za-z0-9_-]+$"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceDoc(BaseModel):
    evidence_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=8, max_length=2048)
    excerpt: str = Field(min_length=20, max_length=6000)
    source_type: Literal["user_url", "wikipedia", "web"] = "web"
    license_note: str = "Source text is for factual grounding; reuse rights must be checked."
    retrieved_at: datetime = Field(default_factory=utc_now)


class Claim(BaseModel):
    claim_id: str = Field(pattern=ID_PATTERN)
    text: str = Field(min_length=3, max_length=600)
    evidence_ids: list[str] = Field(min_length=1)


class Material(BaseModel):
    material_id: str = Field(pattern=ID_PATTERN)
    provider: str = Field(min_length=1, max_length=80)
    source_url: str = Field(min_length=8, max_length=2048)
    download_url: str = Field(default="", max_length=2048)
    preview_image_url: str = Field(default="", max_length=4096)
    local_path: str = Field(default="", max_length=4096)
    title: str = Field(default="", max_length=300)
    author: str = Field(default="", max_length=200)
    license_url: str = Field(default="", max_length=2048)
    duration_s: float = Field(default=0.0, ge=0, le=3600)
    width: int = Field(default=0, ge=0, le=16384)
    height: int = Field(default=0, ge=0, le=16384)
    sha256: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")


class Scene(BaseModel):
    scene_id: str = Field(pattern=ID_PATTERN)
    narration: str = Field(min_length=1, max_length=500)
    visual_query_en: str = Field(min_length=2, max_length=200)
    duration_hint_s: float = Field(default=5.0, ge=1.0, le=20.0)
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    material: Material | None = None
    # 抖音优化：每句 1-2 个视觉强调关键词，用于字幕花字高亮
    keywords: list[str] = Field(default_factory=list)


class VideoPlan(BaseModel):
    topic: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=120)
    hook: str = Field(min_length=1, max_length=300)
    # 黄金 3 秒片头标题（≤12 字，悬念式）
    hook_title: str = Field(default="", max_length=30)
    # 片尾 CTA 文案
    cta: str = Field(default="", max_length=120)
    # 抖音发布话题标签
    hashtags: list[str] = Field(default_factory=list)
    target_audience: str = Field(default="general", max_length=120)
    style: str = Field(default="factual-explainer", max_length=120)
    aspect_ratio: Literal["9:16"] = "9:16"
    estimated_duration_s: float = Field(default=30.0, ge=5.0, le=180.0)
    claims: list[Claim] = Field(min_length=1, max_length=30)
    scenes: list[Scene] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_graph(self) -> "VideoPlan":
        claim_ids = [claim.claim_id for claim in self.claims]
        scene_ids = [scene.scene_id for scene in self.scenes]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id values must be unique")
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError("scene_id values must be unique")

        claims_by_id = {claim.claim_id: claim for claim in self.claims}
        for scene in self.scenes:
            unknown_claims = set(scene.claim_ids) - claims_by_id.keys()
            if unknown_claims:
                raise ValueError(
                    f"scene {scene.scene_id} references unknown claims: {sorted(unknown_claims)}"
                )
            supporting_evidence = {
                evidence_id
                for claim_id in scene.claim_ids
                for evidence_id in claims_by_id[claim_id].evidence_ids
            }
            if not set(scene.evidence_ids).issubset(supporting_evidence):
                raise ValueError(
                    f"scene {scene.scene_id} cites evidence not attached to its claims"
                )
        return self


class Evaluation(BaseModel):
    # These are structural reference-coverage metrics, not semantic truth scores.
    claim_provenance_coverage: float = Field(ge=0, le=1)
    scene_evidence_coverage: float = Field(ge=0, le=1)
    media_coverage: float = Field(ge=0, le=1)
    unsupported_claim_ids: list[str] = Field(default_factory=list)
    reviewer_notes: list[str] = Field(default_factory=list)


class RunManifest(BaseModel):
    run_id: str = Field(pattern=ID_PATTERN)
    topic: str
    status: Literal["planned", "rendered", "failed"]
    planner_provider: str
    reviewer_provider: str = ""
    evidence: list[EvidenceDoc] = Field(default_factory=list)
    plan: VideoPlan
    evaluation: Evaluation | None = None
    # Perceptual rubric report (nullable when rubric iteration is disabled).
    rubric: dict | None = None
    # VL (vision-language) visual-relevance report (nullable when not rendered/disabled).
    vl: dict | None = None
    output_video: str = ""
    subtitle_path: str = ""
    cover_path: str = ""
    publish_meta: dict | None = None
    edit_plan: dict | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
