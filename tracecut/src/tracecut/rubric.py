"""Rubric-based quality evaluation and budget-aware self-iteration.

This is the *perceptual* track, complementing ``evaluate.py``'s *structural*
provenance track. Dimensions (each scored 1-5 with a one-line actionable note):

- relevance:  each scene's visual_query_en actually matches its claim/narration
- flow:       scene-to-scene logical progression, no jumps or repetition
- transition: adjacent scenes' visuals are coherent (no jarring subject switch)
- completeness: hook → claims → a closing that resolves; no orphan claims
- adequacy:   content is sufficient to fill the TARGET duration — enough scenes,
              enough narration, real information density — so it reads as a
              complete short video rather than a 2-scene fragment. This is the
              macro counterpart to completeness (which is structural only).
- tts_match:  narration length/segmentation suits spoken delivery (proxy via text)

Judges run on the same SiliconFlow/Venus OpenAI-compatible gateway already used
for planning/review. The iteration loop only edits *soft* variables — narration
wording, scene order, visual_query, TTS instruction — and NEVER touches claim
text, claim_id/evidence_id references, so the provenance graph invariant is
preserved (re-validated after each edit).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from tracecut.providers.llm import LLMProvider
from tracecut.schemas import EvidenceDoc, VideoPlan


RUBRIC_DIMENSIONS = ["relevance", "flow", "transition", "completeness", "adequacy", "tts_match"]


class RubricScore(BaseModel):
    score: int = Field(ge=1, le=5)
    note: str = Field(default="", max_length=400)


class RubricReport(BaseModel):
    # dimension -> RubricScore
    scores: dict[str, RubricScore] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
    iterations: int = 0
    # the narration/visual_query/order edits applied, for provenance in the manifest
    edits: list[str] = Field(default_factory=list)

    @property
    def average(self) -> float:
        if not self.scores:
            return 0.0
        return sum(s.score for s in self.scores.values()) / len(self.scores)

    def below_threshold(self, threshold: int) -> list[str]:
        return [d for d, s in self.scores.items() if s.score < threshold]


_SYSTEM = (
    "You are a strict short-video quality auditor. Score each dimension 1-5 "
    "(5=excellent, 1=broken) and give a concrete, actionable Chinese note. "
    "Never reward style over factual support. Return ONLY a JSON object."
)

_EDIT_SYSTEM = (
    "You are a short-video script editor. You receive a plan and low-scoring "
    "rubric dimensions. Rewrite the plan to improve those dimensions. "
    "You may ONLY edit: title, hook, scene.narration, scene.visual_query_en, "
    "and scene ORDER. You MUST NOT change claim text, claim_id, evidence_id, "
    "or any claim<->evidence reference, and you MUST NOT add or remove claims "
    "or scenes. "
    "Return ONLY one JSON object that is a COMPLETE VideoPlan with these "
    "top-level keys: topic, title, hook, target_audience, style, aspect_ratio, "
    "estimated_duration_s, claims[], scenes[]. Do NOT return scores or notes. "
    "Each scene keeps its original scene_id/claim_ids/evidence_ids unchanged; "
    "you may only change its narration, visual_query_en, and position in the array."
)


def plan_digest(plan: VideoPlan) -> str:
    """Compact, judge-friendly view of the plan (no evidence excerpts)."""
    scenes = [
        {
            "scene_id": s.scene_id,
            "narration": s.narration,
            "visual_query_en": s.visual_query_en,
            "claim_ids": s.claim_ids,
            "evidence_ids": s.evidence_ids,
        }
        for s in plan.scenes
    ]
    claims = [
        {"claim_id": c.claim_id, "text": c.text, "evidence_ids": c.evidence_ids}
        for c in plan.claims
    ]
    return json.dumps(
        {
            "title": plan.title,
            "hook": plan.hook,
            "topic": plan.topic,
            "claims": claims,
            "scenes": scenes,
        },
        ensure_ascii=False,
    )


def check_soft_edit_invariants(
    old_plan: VideoPlan,
    new_plan: VideoPlan,
    max_chars: int | None = None,
) -> str | None:
    """Validate that new_plan only changed soft variables.

    Returns an error message if the edit violates invariants, otherwise None.
    Soft edits allowed: title, hook, hook_title, cta, hashtags, scene.narration,
    scene.visual_query_en, scene.keywords, scene order.
    Frozen: topic, aspect_ratio, estimated_duration_s, target_audience, style,
    claim text, claim_id, evidence_id, claim<->evidence refs, scene_id count,
    number of claims.
    """
    if old_plan.topic != new_plan.topic:
        return "topic changed"
    if old_plan.aspect_ratio != new_plan.aspect_ratio:
        return "aspect_ratio changed"
    if old_plan.estimated_duration_s != new_plan.estimated_duration_s:
        return "estimated_duration_s changed"
    if old_plan.target_audience != new_plan.target_audience:
        return "target_audience changed"
    if old_plan.style != new_plan.style:
        return "style changed"

    old_claims = {(c.claim_id, c.text, tuple(c.evidence_ids)) for c in old_plan.claims}
    new_claims = {(c.claim_id, c.text, tuple(c.evidence_ids)) for c in new_plan.claims}
    if old_claims != new_claims:
        return "claim text/id/evidence references changed"

    old_scene_ids = {s.scene_id for s in old_plan.scenes}
    new_scene_ids = {s.scene_id for s in new_plan.scenes}
    if old_scene_ids != new_scene_ids:
        return "scene_id set changed"

    old_by_id = {s.scene_id: s for s in old_plan.scenes}
    new_by_id = {s.scene_id: s for s in new_plan.scenes}
    for sid in old_scene_ids:
        old_scene = old_by_id[sid]
        new_scene = new_by_id[sid]
        if old_scene.claim_ids != new_scene.claim_ids:
            return f"scene {sid} claim_ids changed"
        if old_scene.evidence_ids != new_scene.evidence_ids:
            return f"scene {sid} evidence_ids changed"
        if len(new_scene.narration) > 500:
            return f"scene {sid} narration too long"

    if max_chars is not None:
        total = sum(len(s.narration) for s in new_plan.scenes)
        if total > int(max_chars * 1.5):
            return f"total narration {total} chars exceeds budget {max_chars}"
    return None


def score_rubric(
    judge: LLMProvider,
    plan: VideoPlan,
    evidence: list[EvidenceDoc],
) -> RubricReport:
    """Ask the judge to score all rubric dimensions for the current plan."""
    total_narration_chars = sum(len(s.narration) for s in plan.scenes)
    prompt = {
        "task": "Score this short-video plan on 6 rubric dimensions.",
        "duration_context": {
            "target_duration_s": plan.estimated_duration_s,
            "scene_count": len(plan.scenes),
            "total_narration_chars": total_narration_chars,
            "note": (
                "目标时长是视频应填满的总秒数。按 ~4.2 字/秒估算，当前旁白总长约可支撑 "
                f"{round(total_narration_chars / 4.2, 1)} 秒口播。adequacy 维度据此判断内容是否足够填满目标时长。"
            ),
        },
        "dimensions": {
            "relevance": "每个 scene 的 visual_query_en 是否真的匹配其 claim/narration 的含义",
            "flow": "scene 之间的逻辑是否递进，有无跳跃或重复",
            "transition": "相邻 scene 的画面主体是否连贯，有无突兀跳变",
            "completeness": "hook→claims→结尾是否完整收束，有无未展开的 claim（结构性闭合）",
            "adequacy": (
                "相对目标时长，内容是否充分：scene 数量是否足够、旁白总长是否能填满目标时长、"
                "信息密度是否合理、是否像一个完整短视频而不是片段。"
                "若 scene 过少或旁白远不足以撑起目标时长，必须给低分（≤2）。"
            ),
            "tts_match": "旁白长度与断句是否适合口播（不过长、可一口气读完）",
        },
        "instruction": (
            "Return JSON: {\"scores\": {dim: {\"score\": int, \"note\": str}, ...}, "
            "\"suggestions\": [str, ...]}. suggestions 是面向改写旁白/调整顺序/换视觉查询的可执行建议，"
            "不得要求新增 claim 或改动 evidence 引用。"
        ),
        "plan": json.loads(plan_digest(plan)),
    }
    raw = judge.complete_json(
        system=_SYSTEM,
        user=json.dumps(prompt, ensure_ascii=False),
        max_tokens=2400,
    )
    report = RubricReport()
    scores_raw = raw.get("scores", {}) or {}
    for dim in RUBRIC_DIMENSIONS:
        entry = scores_raw.get(dim)
        # Tolerate several shapes: {"score":4,"note":"..."}, {"4":"..."}, bare int, or None.
        try:
            if isinstance(entry, dict):
                score = int(entry.get("score", 3))
                note = str(entry.get("note", ""))[:400]
            elif isinstance(entry, (int, float)):
                score, note = int(entry), ""
            else:
                score, note = 3, ""
            report.scores[dim] = RubricScore(score=score, note=note)
        except Exception:
            report.scores[dim] = RubricScore(score=3, note="parse error")
    report.suggestions = [str(s) for s in raw.get("suggestions", []) if str(s).strip()][:8]
    return report


@dataclass
class IterationConfig:
    max_iters: int = 2
    threshold: int = 4  # re-iterate while any dimension < threshold
    judge_provider: str = "siliconflow"


@dataclass
class IterationResult:
    plan: VideoPlan
    report: RubricReport
    history: list[RubricReport] = field(default_factory=list)


def _apply_soft_edit(
    provider: LLMProvider,
    plan: VideoPlan,
    report: RubricReport,
    evidence: list[EvidenceDoc],
) -> tuple[VideoPlan, str]:
    """Ask the model to rewrite SOFT variables only, preserving the provenance graph.

    Returns (new_plan, reason). If the model output fails schema validation or
    tries to change frozen references, the original plan is returned unchanged
    with a reason describing why the edit was rejected.
    """
    low_dims = report.below_threshold(4)
    if not low_dims:
        return plan, "no low dimensions"
    # Keep narration within the spoken-time budget so edits for "adequacy" don't
    # overshoot and make TTS exceed the target video length.
    target_chars = max(20, int(plan.estimated_duration_s * 4.2))
    max_chars = int(target_chars * 1.15)
    min_chars = int(target_chars * 0.85)
    prompt = {
        "task": "Rewrite the plan below to improve the low-scoring rubric dimensions.",
        "low_scoring_dimensions": low_dims,
        "dimension_notes": {d: report.scores[d].note for d in low_dims},
        "suggestions": report.suggestions,
        "constraints": (
            "Editable: title, hook, scene.narration, scene.visual_query_en, scene order. "
            "Frozen (do not change): claim text, claim_id, evidence_id, claim<->evidence refs, "
            "scene_id, number of claims, number of scenes. "
            f"NARRATION BUDGET: total narration across all scenes MUST stay between {min_chars} and "
            f"{max_chars} chars (~4.2 chars per spoken second for a {plan.estimated_duration_s:.0f}s video). "
            "Do NOT lengthen narration beyond the budget to 'fix' adequacy; improve density/ordering instead."
        ),
        "plan": json.loads(plan_digest(plan)),
        "required_output": (
            "A single JSON object that is a COMPLETE VideoPlan with keys: topic, title, hook, "
            "target_audience, style, aspect_ratio, estimated_duration_s, claims[], scenes[]. "
            "Copy the claims[] array verbatim from the input plan. Re-emit ALL scenes (with "
            "possibly new narration/visual_query_en/order). Do NOT include scores or notes."
        ),
    }
    raw = provider.complete_json(
        system=_EDIT_SYSTEM,
        user=json.dumps(prompt, ensure_ascii=False),
        max_tokens=3200,
    )
    # Preserve hard fields the model must not change, then re-validate the graph.
    raw.setdefault("topic", plan.topic)
    raw["aspect_ratio"] = "9:16"
    raw.setdefault("estimated_duration_s", plan.estimated_duration_s)
    raw.setdefault("target_audience", plan.target_audience)
    raw.setdefault("style", plan.style)
    try:
        new_plan = VideoPlan.model_validate(raw)
    except Exception as exc:
        return plan, f"model output failed schema validation: {str(exc)[:120]}"
    # Guard: claim/evidence references must be byte-identical to before.
    old_refs = {(c.claim_id, tuple(c.evidence_ids)) for c in plan.claims}
    new_refs = {(c.claim_id, tuple(c.evidence_ids)) for c in new_plan.claims}
    if old_refs != new_refs:
        return plan, "model tried to change claim/evidence references"
    # Narration budget guard: only reject truly runaway over-writing. Timing is
    # content-driven now, so a richer narration is fine; just stop pathological bloat.
    new_total = sum(len(s.narration) for s in new_plan.scenes)
    if new_total > int(max_chars * 1.5):
        return plan, f"narration ballooned to {new_total} chars (budget {max_chars})"
    # The editor only sees a material-free digest, so re-attach each scene's
    # previously downloaded material by scene_id. Editing narration/visual_query
    # must not silently drop the B-roll that Pexels already fetched.
    materials = {s.scene_id: s.material for s in plan.scenes if s.material}
    for scene in new_plan.scenes:
        if scene.scene_id in materials and not scene.material:
            scene.material = materials[scene.scene_id]
    # Fields the editor is neither allowed nor asked to change: the edit prompt
    # does not list hook_title/cta/hashtags (or scene.keywords), so a compliant
    # model omits them and they silently reset to ""/[]. That blanked the title
    # card, the CTA card, the hashtag line and ALL subtitle keyword highlighting.
    # Re-attach them from the original plan, same as the material above.
    if not new_plan.hook_title:
        new_plan.hook_title = plan.hook_title
    if not new_plan.cta:
        new_plan.cta = plan.cta
    if not new_plan.hashtags:
        new_plan.hashtags = plan.hashtags
    old_scenes = {s.scene_id: s for s in plan.scenes}
    for scene in new_plan.scenes:
        if not scene.keywords and scene.scene_id in old_scenes:
            scene.keywords = old_scenes[scene.scene_id].keywords
    return new_plan, "applied"


def iterate_with_rubric(
    provider: LLMProvider,
    judge: LLMProvider,
    plan: VideoPlan,
    evidence: list[EvidenceDoc],
    config: IterationConfig | None = None,
) -> IterationResult:
    """Run rubric scoring + soft-edit iteration until all dims >= threshold or budget exhausted."""
    cfg = config or IterationConfig()
    history: list[RubricReport] = []
    current = plan
    edits: list[str] = []
    for i in range(cfg.max_iters + 1):
        report = score_rubric(judge, current, evidence)
        report.iterations = i
        history.append(report)
        low = report.below_threshold(cfg.threshold)
        if not low or i == cfg.max_iters:
            report.edits = edits
            return IterationResult(plan=current, report=report, history=history)
        prev = current
        current, reason = _apply_soft_edit(provider, prev, report, evidence)
        if current is prev:
            edits.append(f"iter {i+1}: edit rejected — {reason}")
        else:
            edits.append(f"iter {i+1}: applied soft edit for {low}")
    history[-1].edits = edits
    return IterationResult(plan=current, report=history[-1], history=history)
