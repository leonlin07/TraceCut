from __future__ import annotations

import json

from tracecut.providers.llm import LLMProvider
from tracecut.schemas import Evaluation, EvidenceDoc, VideoPlan


def deterministic_evaluation(plan: VideoPlan, evidence: list[EvidenceDoc]) -> Evaluation:
    evidence_ids = {item.evidence_id for item in evidence}
    unsupported = []
    supported_claims = 0
    for claim in plan.claims:
        valid = bool(claim.evidence_ids) and set(claim.evidence_ids).issubset(evidence_ids)
        if valid:
            supported_claims += 1
        else:
            unsupported.append(claim.claim_id)

    valid_scenes = sum(
        1
        for scene in plan.scenes
        if scene.evidence_ids and set(scene.evidence_ids).issubset(evidence_ids)
    )
    media_scenes = sum(1 for scene in plan.scenes if scene.material and scene.material.local_path)
    return Evaluation(
        claim_provenance_coverage=(supported_claims / len(plan.claims)) if plan.claims else 0.0,
        scene_evidence_coverage=(valid_scenes / len(plan.scenes)) if plan.scenes else 0.0,
        media_coverage=(media_scenes / len(plan.scenes)) if plan.scenes else 0.0,
        unsupported_claim_ids=unsupported,
    )


def review_plan(
    reviewer: LLMProvider,
    plan: VideoPlan,
    evidence: list[EvidenceDoc],
) -> list[str]:
    prompt = {
        "task": "Find factual overreach, citation mismatches, and weak scene-to-evidence links.",
        "instruction": "Return JSON with a reviewer_notes array of concise Chinese strings.",
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "plan": plan.model_dump(mode="json"),
    }
    # LLM 偶发返回非 JSON（thinking 泄漏 / 空响应）会抛 ValueError；重试几次
    # 而不是让 reviewer 直接失败、中断整条流水线。
    last_error: Exception | None = None
    for _ in range(3):
        try:
            raw = reviewer.complete_json(
                system="You are a strict evidence auditor. Never reward style over factual support.",
                user=json.dumps(prompt, ensure_ascii=False),
                max_tokens=1200,
            )
            notes = raw.get("reviewer_notes", [])
            return [str(note) for note in notes if str(note).strip()][:12]
        except ValueError as exc:  # noqa: PERF203 - retry on malformed output
            last_error = exc
            continue
    raise last_error  # type: ignore[misc]
