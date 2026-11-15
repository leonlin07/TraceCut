"""tracecut.editing.loop — run_editing_agent().

A real multi-turn tool-calling loop: the model sees the rubric feedback and
storyboard, calls edit/render tools over several turns (tool results are
appended as role:"tool" messages so it can see its own decisions), then calls
finish. Turn-capped so a looping model can't burn budget unboundedly.

Degradation ladder — the agent must NEVER block the video pipeline:
  1. tool-calling loop (chat_with_tools);
  2. ToolCallingUnsupported → one single-shot complete_json asking for the
     same decisions as a JSON object, run through the same guards;
  3. any failure → empty EditPlan, unmodified plan, warning appended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tracecut.editing.plan import EditPlan
from tracecut.editing.tools import EditingSession, build_tool_schemas
from tracecut.providers.llm import LLMProvider, ToolCallingUnsupported
from tracecut.rubric import RubricReport, plan_digest
from tracecut.schemas import EvidenceDoc, VideoPlan

log = logging.getLogger(__name__)

# The Director now owns B-roll selection (search/attach/rerank) on top of
# content+render edits, so one revision round needs a larger turn budget than
# the 12-turn cap that sufficed when it only rewrote narration and set a
# transition or two.
_MAX_TURNS = 30

_SYSTEM_PROMPT = """你是一名短视频剪辑师，服务于一个"证据可追溯"的事实解说视频生成系统。
你会收到：当前分镜计划、rubric 评分中低于阈值的维度及改进建议。你的工作分两层：

【内容修改】可改 title/hook、各分镜的 narration 和 visual_query_en、分镜顺序，
也可以在 completeness/adequacy/flow 低分时用 add_scene/remove_scene 调整分镜结构。
**结构限制**：add_scene 只能引用当前计划里已有的 claim_id/evidence_id（不能新增事实）；
remove_scene 不能把导致某 claim 仅剩它一个引用分镜、也不能删到低于下限。
绝不允许碰 claim 文本、claim_id、evidence_id 及 claim↔evidence 引用（系统硬性拒绝）。

【硬性规则】
1. adequacy 低分时**必须**通过 edit_scene 调整 narration 的长度与信息密度来修复，
   不要只改 visual_query_en。**严格按 judge 给出的 suggestions 走**：suggestions 让
   加长就加长（增加过程细节/具体数据/连接词），让删减就删减（合并冗余句、去掉
   重复描述）。不要反向操作。当前口播预算 ~4.2 字/秒 + 15% 冗余，总字数应落在
   目标时长对应的字数附近（目标秒数 × 4.2）。
2. visual_query_en 低分/relevance 低分时，把检索词改成**证据文本中描述的、可以被
   拍到的具体视觉主体**（例如"chloroplast close-up""leaf cross-section""forest
   canopy sunlight"），而不是抽象隐喻（"bubbles""forest"这种纯意象词）。
3. completeness/adequacy 低分且 judge 建议"补一个承接/收束镜头"时，优先 add_scene
   而不只是拉长现有分镜旁白；judge 说"重复/冗余"时优先 remove_scene 或合并。
4. relevance / misleading_visual 低分时，先 search_broll 按第 2 条改写检索词找候选
   （可以用 rerank_broll 按 narration 重排序），再 attach_broll 换素材——不要只留一个
   edit_scene 改 visual_query 了事。
5. 只要还有维度低于阈值、且还有可用资产，就**主动**在关键节拍上用至少一项渲染
   决策（set_transition / set_emphasis_caption / place_bgm / place_sfx），不要把
   edit_plan 留空。花字/转场不要每镜都加，只在真正值得强调的节拍上加。

【渲染决策】转场（大部分切点保持硬切，只在情绪转折的关键节拍用）、强调花字
（只在真正值得强调的信息上用）、BGM/SFX（如可用）。transition 维度低分时优先
考虑 set_transition；视觉主体跳变严重时也可以用 edit_scene 调整 visual_query_en
让相邻画面更连贯。

完成后调用 finish。"""


@dataclass
class EditingResult:
    plan: VideoPlan
    edit_plan: EditPlan
    applied: list[str] = field(default_factory=list)
    degraded: str = ""  # non-empty when a fallback path was taken


def _revision_context(report: RubricReport, threshold: int) -> str:
    low_dims = report.below_threshold(threshold)
    lines = [f"═══ rubric 评分反馈（平均 {report.average:.1f}/5）═══", "低分维度："]
    for dim in low_dims:
        note = report.scores[dim].note
        lines.append(f"- {dim} ({report.scores[dim].score}/5): {note}")
    if report.suggestions:
        lines.append("改进建议：")
        lines += [f"- {s}" for s in report.suggestions]
    lines.append("请针对上述低分维度做修改，不要重复上次的错误。")
    return "\n".join(lines)


def run_editing_agent(
    provider: LLMProvider,
    plan: VideoPlan,
    evidence: list[EvidenceDoc],  # noqa: ARG001 — kept for signature symmetry with score_rubric
    report: RubricReport,
    *,
    threshold: int = 4,
    pexels_source=None,
    vl_rerank_provider=None,
) -> EditingResult:
    """Run one revision round. Any failure degrades to a no-op EditingResult.

    `pexels_source` / `vl_rerank_provider` enable the Director's B-roll tools;
    when None the agent sees only content+render tools (its media capability is
    simply absent, not an error)."""
    schemas, bgm_tracks, sfx_tracks, _has_media, _has_vl = build_tool_schemas(
        pexels_source, vl_rerank_provider
    )
    session = EditingSession.for_plan(
        plan,
        bgm_tracks=bgm_tracks,
        sfx_tracks=sfx_tracks,
        pexels_source=pexels_source,
        vl_rerank_provider=vl_rerank_provider,
    )
    user_prompt = (
        f"{_revision_context(report, threshold)}\n\n当前计划：\n{plan_digest(plan)}"
    )
    try:
        result = _run_tool_loop(provider, session, schemas, user_prompt)
        if result is not None:
            log.info("[editing] tool 循环完成: %s", "; ".join(result.applied) or "(无修改)")
            return result
        log.warning("[editing] tool 循环失败，降级到单次结构化输出")
    except ToolCallingUnsupported as exc:
        log.warning("[editing] 提供方不支持 tool-calling (%s)，降级到单次结构化输出", exc)
    except Exception as exc:  # noqa: BLE001 — degradation is the design
        log.warning("[editing] tool 循环异常 (%s)，降级到单次结构化输出", exc)

    return _run_single_shot(provider, session, user_prompt)


def _run_tool_loop(
    provider: LLMProvider,
    session: EditingSession,
    schemas: list[dict],
    user_prompt: str,
) -> EditingResult | None:
    """Returns None if the loop produced nothing usable (caller degrades)."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    for _ in range(_MAX_TURNS):
        assistant_message, tool_calls = provider.chat_with_tools(
            messages=messages, tools=schemas, max_tokens=1200
        )
        if not tool_calls:
            # Model answered with plain text: treat as "no more decisions".
            break
        messages.append(assistant_message)
        done = False
        for call in tool_calls:
            result_text, is_finish = session.dispatch(call.name, call.arguments)
            messages.append(
                {"role": "tool", "tool_call_id": call.call_id, "content": result_text}
            )
            done = done or is_finish
        if done:
            break
    else:
        log.info("[editing] 达到最大轮次 %d，结束循环", _MAX_TURNS)
    return EditingResult(
        plan=session.plan, edit_plan=session.edit_plan, applied=session.applied
    )


_SINGLE_SHOT_PROMPT = """你是短视频剪辑师。根据下面的 rubric 反馈修改视频计划。

{user_prompt}

严格输出一个 JSON 对象（不要任何额外说明、不要 markdown 代码块）：
{{
  "content_edits": {{
    "title": "新标题（可选）",
    "hook": "新 hook（可选）",
    "scenes": [{{"scene_id": "...", "narration": "...", "visual_query_en": "..."}}],
    "add_scenes": [{{"scene_id": "s_new", "insert_after": "s_xx", "narration": "...", "visual_query_en": "...", "claim_ids": ["c_x"], "evidence_ids": ["ev_x"]}}],
    "remove_scene_ids": ["s_xx"],
    "scene_order": ["scene_id", "..."]
  }},
  "edit_plan": {{
    "transitions": [{{"scene_id": "...", "kind": "fade|slideleft|slideright|wipeleft|circleopen", "duration_s": 0.4}}],
    "captions": [{{"scene_id": "...", "text": "...", "position": "top|center", "start_frac": 0.0, "end_frac": 1.0}}],
    "bgm": {{"track": "...", "volume": 0.15}},
    "sfx": [{{"scene_id": "...", "track": "...", "offset_s": 0.0, "volume": 0.8}}]
  }}
}}

约束：不允许改 claim/evidence 引用；content_edits 和 edit_plan 的所有字段都可省略；
空修改输出 {{"content_edits": {{}}, "edit_plan": {{}}}}。转场不要设在第一个分镜上。
若 adequacy 低分，必须先按 suggestions 方向调整 content_edits.scenes 的 narration
（让加长就加长、让删减就删减），不能只改 visual_query_en。"""


def _run_single_shot(
    provider: LLMProvider,
    session: EditingSession,
    user_prompt: str,
) -> EditingResult:
    """Degraded path: one JSON completion, decisions replayed through the same
    guarded dispatch so fidelity rules apply identically."""
    degraded = EditingResult(
        plan=session.plan,
        edit_plan=EditPlan(),
        degraded="tool-calling unavailable; single-shot fallback failed",
    )
    try:
        raw = provider.complete_json(
            system="你只输出 JSON。",
            user=_SINGLE_SHOT_PROMPT.format(user_prompt=user_prompt),
            max_tokens=2400,
            temperature=0.4,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[editing] 单次结构化输出失败 (%s)，本轮不修改", exc)
        return degraded

    edits = raw.get("content_edits") or {}
    if not isinstance(edits, dict):
        return degraded
    if edits.get("title") is not None or edits.get("hook") is not None:
        session.dispatch(
            "edit_title_hook", {"title": edits.get("title"), "hook": edits.get("hook")}
        )
    for scene_edit in edits.get("scenes") or []:
        if isinstance(scene_edit, dict):
            session.dispatch("edit_scene", scene_edit)
    for add in edits.get("add_scenes") or []:
        if isinstance(add, dict):
            session.dispatch("add_scene", add)
    for scene_id in edits.get("remove_scene_ids") or []:
        session.dispatch("remove_scene", {"scene_id": str(scene_id)})
    if edits.get("scene_order"):
        session.dispatch("reorder_scenes", {"scene_ids": edits["scene_order"]})

    edit_plan_raw = raw.get("edit_plan") or {}
    if isinstance(edit_plan_raw, dict):
        for t in edit_plan_raw.get("transitions") or []:
            if isinstance(t, dict):
                session.dispatch("set_transition", t)
        for c in edit_plan_raw.get("captions") or []:
            if isinstance(c, dict):
                session.dispatch("set_emphasis_caption", c)
        bgm = edit_plan_raw.get("bgm")
        if isinstance(bgm, dict):
            session.dispatch("place_bgm", bgm)
        for s in edit_plan_raw.get("sfx") or []:
            if isinstance(s, dict):
                session.dispatch("place_sfx", s)

    log.info("[editing] 单次结构化输出完成: %s", "; ".join(session.applied) or "(无修改)")
    return EditingResult(
        plan=session.plan,
        edit_plan=session.edit_plan,
        applied=session.applied,
        degraded="tool-calling unavailable; used single-shot fallback",
    )
