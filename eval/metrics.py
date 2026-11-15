"""语义评测 + 结构指标汇总。

忠实度（faithfulness）用 LLM-as-judge 对照证据与人工标注主张打分；相关性
（relevance）与误导性（misleading）复用 manifest 里已有的 VL 视觉评判结果；
结构指标（coverage）复用 manifest 里的 deterministic evaluation。
"""

from __future__ import annotations

import json
from typing import Any

from eval.dataset.topics import EvalCase

_FAITHFULNESS_SYSTEM = (
    "你是一个事实忠实度评审员。你会收到：证据文档摘要、人工标注的关键主张、"
    "以及模型生成的 claims。评分 1-5（5=完全忠于证据，1=严重偏离/无依据推断）。"
    "只返回 JSON：{\"score\": int, \"note\": \"中文评语\"}。"
)


def score_faithfulness(
    provider, case: EvalCase, manifest: dict[str, Any]
) -> dict[str, Any]:
    """用 LLM 对照证据与人工标注主张，评估生成 claims 的事实忠实度。"""
    plan = manifest.get("plan", {})
    claims = [c.get("text", "") for c in plan.get("claims", [])]
    evidence = [e.get("excerpt", "")[:2000] for e in manifest.get("evidence", [])]
    prompt = {
        "task": "评估生成主张的事实忠实度",
        "generated_claims": claims,
        "evidence_excerpts": evidence,
        "expected_claims": case.expected_claims,
        "instruction": (
            "对照证据与预期主张，评估生成 claims 是否忠于证据、有无无依据推断。"
        ),
    }
    raw = provider.complete_json(
        system=_FAITHFULNESS_SYSTEM,
        user=json.dumps(prompt, ensure_ascii=False),
        max_tokens=300,
    )
    score = max(1, min(5, int(raw.get("score", 3))))
    return {"score": score, "note": str(raw.get("note", ""))[:300]}


def aggregate_metrics(
    case: EvalCase,
    manifest: dict[str, Any],
    faithfulness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """汇总单个 case 的结构指标 + 语义指标，供报告/消融使用。"""
    evaluation = manifest.get("evaluation", {})
    vl = manifest.get("vl") or {}
    scores = vl.get("scores", [])
    misleading_count = sum(1 for s in scores if s.get("misleading_visual"))
    return {
        "case_id": case.id,
        "topic": case.topic,
        "difficulty": case.difficulty,
        # 结构指标（复用 deterministic_evaluation）
        "claim_provenance_coverage": evaluation.get("claim_provenance_coverage"),
        "scene_evidence_coverage": evaluation.get("scene_evidence_coverage"),
        "media_coverage": evaluation.get("media_coverage"),
        # 语义指标
        "faithfulness": faithfulness,
        "vl_relevance_avg": vl.get("average"),
        "misleading_ratio": (misleading_count / len(scores)) if scores else None,
    }
