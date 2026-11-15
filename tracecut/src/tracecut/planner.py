from __future__ import annotations

import json
import math

from pydantic import ValidationError

from tracecut.providers.llm import LLMProvider
from tracecut.schemas import EvidenceDoc, VideoPlan


_SYSTEM = """You are the planning component of TraceCut, a factual short-video editor.
Create a concise vertical-video plan grounded only in the supplied evidence documents.
Every factual claim MUST cite one or more supplied evidence_id values.
Every scene MUST cite claim_id and evidence_id values and include a short English stock-footage query.
A scene's evidence_ids MUST be a subset of the evidence_ids attached to the claims it cites.
Do not invent statistics, quotes, dates, people, or causal claims.
Do not invent evidence_id values that are not in the supplied evidence documents.
If the evidence is weak, make fewer and narrower claims, but still meet the target scene count.

NARRATION QUALITY: each scene's narration is a FULL, spoken Chinese sentence (or two
short ones), ~25-55 characters — never a terse label like "该模型由 Moonshot AI 开发".
Write the way a narrator would actually say it on camera: connective, concrete,
informative. The scenes together must form a story arc: a hook that names the news,
a body that explains what/why/how grounded in evidence, and a closing that lands the
significance. The video length is driven by how much you write, so write enough to
tell the story — do not starve the narration to hit a duration target.

VISUAL QUERIES: make them specific to the scene's content (not generic "office
building" / "person coding"). Prefer concrete nouns that a stock-footage search can
match to the claim.

DOUYIN OPTIMIZATION:
- hook_title: a suspenseful opening title ≤12 Chinese characters that grabs attention in the first 3 seconds (e.g., "AI 会骗你吗？").
- cta: a 2.5-second closing call-to-action, e.g., "关注我，3 分钟看懂 XX" plus one short topic tag.
- hashtags: 3-5 Douyin-style tags like #AI #科普, derived from the topic.
- keywords: for each scene, pick 1-2 Chinese keywords (2-6 chars, noun preferred) to highlight in the subtitle.

Return one JSON object matching the requested schema, with no markdown."""

# How many times to re-prompt the planner when its output violates the provenance
# graph invariants OR the duration budget. Each retry feeds the concrete error
# back so the model can repair the plan rather than aborting the whole run.
_MAX_PLAN_ATTEMPTS = 3

# Narration pacing: ~4.2 Chinese characters per spoken second. Used to translate a
# target video duration into a target narration length so the planner produces
# enough content to actually fill the video (rather than 2 terse scenes for a 40s
# request).
_CHARS_PER_SECOND = 4.2
_SECONDS_PER_SCENE = 7.0


def _duration_budget(duration_s: int) -> tuple[int, int, int]:
    """Return (target_scene_count, min_narration_chars, max_narration_chars) for a
    video of `duration_s` seconds. Timing is content-driven, so this is now soft
    guidance: a scene-count floor so the plan isn't a 2-scene fragment, a narration
    floor so scenes are real sentences not labels, and a loose ceiling only to stop
    runaway over-writing. The render no longer forces the video to `duration_s`."""
    target_scenes = max(1, math.ceil(duration_s / _SECONDS_PER_SCENE))
    target_chars = max(20, int(duration_s * _CHARS_PER_SECOND))
    # Keep the ceiling tight enough that narration doesn't blow past the target
    # video length (2.0x let a 30s request balloon to ~60s of speech); 1.4x still
    # leaves headroom for a full story arc without starving it.
    max_chars = int(target_chars * 1.4)
    return target_scenes, target_chars, max_chars


def make_plan(
    provider: LLMProvider,
    *,
    topic: str,
    evidence: list[EvidenceDoc],
    duration_s: int,
    target_audience: str,
    style: str,
) -> VideoPlan:
    evidence_payload = [
        {
            "evidence_id": item.evidence_id,
            "title": item.title,
            "url": item.url,
            "excerpt": item.excerpt[:3500],
        }
        for item in evidence
    ]
    valid_evidence_ids = [item["evidence_id"] for item in evidence_payload]
    target_scenes, min_chars, max_chars = _duration_budget(duration_s)
    per_scene_chars = max(15, int(min_chars / target_scenes))
    schema = {
        "topic": topic,
        "title": "short Chinese title",
        "hook": "first sentence",
        "hook_title": "≤12字悬念标题",
        "cta": "关注我，3 分钟看懂 XX",
        "hashtags": ["#话题", "#科普"],
        "target_audience": target_audience,
        "style": style,
        "aspect_ratio": "9:16",
        "estimated_duration_s": duration_s,
        "claims": [
            {"claim_id": "c_01", "text": "claim", "evidence_ids": ["ev_x"]}
        ],
        "scenes": [
            {
                "scene_id": "s_01",
                "narration": "Chinese narration",
                "visual_query_en": "English stock video query",
                "duration_hint_s": 7,
                "claim_ids": ["c_01"],
                "evidence_ids": ["ev_x"],
                "keywords": ["关键词"],
            }
        ],
    }
    user = (
        f"Topic: {topic}\n"
        f"Target duration: ~{duration_s} seconds (rough guidance — the video length is "
        f"driven by how much narration you write, so do NOT pad or starve to hit it).\n"
        f"Structure budget: produce AT LEAST {target_scenes} scenes. Each scene's narration "
        f"is a full spoken sentence, ~25-55 chars. Total narration across scenes should be "
        f"at least {min_chars} chars (roughly {per_scene_chars}/scene); do not exceed "
        f"{max_chars} chars total.\n"
        f"Tell a complete story: hook → grounded body → closing.\n"
        f"Each scene's duration_hint_s should be ~7 (range 5-10).\n"
        f"Target audience: {target_audience}\n"
        f"Style: {style}\n"
        f"Valid evidence_id values (use ONLY these): {valid_evidence_ids}\n"
        f"Output schema example: {json.dumps(schema, ensure_ascii=False)}\n"
        f"Evidence documents: {json.dumps(evidence_payload, ensure_ascii=False)}"
    )

    last_error = ""
    for attempt in range(_MAX_PLAN_ATTEMPTS):
        prompt = user
        if last_error:
            prompt = (
                f"{user}\n\n"
                f"Your previous plan was rejected:\n"
                f"{last_error}\n"
                f"Fix it and return a corrected plan. Use ONLY evidence_id values "
                f"from the valid list, make sure every scene's evidence_ids are "
                f"attached to that scene's own claims, AND meet the duration budget "
                f"(at least {target_scenes} scenes, total narration between {min_chars} and {max_chars} chars)."
            )
        raw = provider.complete_json(system=_SYSTEM, user=prompt)
        raw.setdefault("topic", topic)
        raw.setdefault("target_audience", target_audience)
        raw.setdefault("style", style)
        raw["aspect_ratio"] = "9:16"
        raw.setdefault("estimated_duration_s", duration_s)
        try:
            plan = VideoPlan.model_validate(raw)
        except ValidationError as exc:
            last_error = str(exc)[:1200]
            continue
        # Duration-budget check: enough scenes and enough narration to actually
        # fill the target video length, but not so much that TTS blows past it.
        gap = _duration_gap(plan, target_scenes, min_chars, max_chars)
        if gap:
            last_error = gap
            continue
        # Force the plan's target to the requested duration (the model sometimes
        # emits its own estimate; the render path uses this to size the video).
        plan = plan.model_copy(update={"estimated_duration_s": float(duration_s)})
        return plan
    raise RuntimeError(f"Planner failed to produce a valid plan after {_MAX_PLAN_ATTEMPTS} attempts: {last_error}")


def _duration_gap(
    plan: VideoPlan, target_scenes: int, min_chars: int, max_chars: int
) -> str:
    """Return a description of how the plan falls outside the duration budget, or
    an empty string if it meets the budget."""
    n_scenes = len(plan.scenes)
    total_chars = sum(len(s.narration) for s in plan.scenes)
    problems = []
    if n_scenes < target_scenes:
        problems.append(
            f"only {n_scenes} scenes, need at least {target_scenes}"
        )
    if total_chars < min_chars:
        problems.append(
            f"total narration only {total_chars} chars, need at least {min_chars}"
        )
    if total_chars > max_chars:
        problems.append(
            f"total narration {total_chars} chars is too long (max {max_chars}); "
            f"TTS would exceed the target video length — shorten the narration"
        )
    return "; ".join(problems)
