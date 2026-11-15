"""tracecut.editing.tools — tool schemas + guarded dispatch for the editing agent.

Two kinds of tools:

- Content edits (edit_scene / edit_title_hook / reorder_scenes / add_scene /
  remove_scene) mutate the working VideoPlan. Each is validated IMMEDIATELY:
  apply to a deep copy, re-run VideoPlan schema+graph validation, then
  rubric.check_soft_edit_invariants; on failure the plan is untouched and the
  rejection reason is returned as the tool result so the model can retry.
- Material selection (search_broll / attach_broll / rerank_broll /
  get_scene_media) let the agent own which clip lands on each scene — the work
  that used to be the pipeline's bespoke VL-reselect loop. Registered only when
  a PexelsSource is configured.
- Render decisions (set_transition / set_emphasis_caption / place_bgm /
  place_sfx) accumulate into an EditPlan. place_bgm/place_sfx are only
  registered when their asset dirs are configured — a tool the model cannot
  see is a tool it cannot hallucinate.

Fidelity > polish: no tool can touch claim text, claim_id, evidence_id, or
claim<->evidence references. add/remove_scene change scene STRUCTURE but must
still cite existing claims/evidence — the provenance graph is never broken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from tracecut.editing.plan import (
    BGM_DIR_ENV,
    SFX_DIR_ENV,
    BgmSpec,
    EditPlan,
    EmphasisCaption,
    SfxSpec,
    TransitionSpec,
    available_tracks,
)
from tracecut.rubric import check_soft_edit_invariants, plan_digest
from tracecut.schemas import Material, Scene, VideoPlan


def _content_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "edit_scene",
                "description": (
                    "修改某个分镜的旁白和/或英文视觉查询词。两者通常需要一起改"
                    "（旁白变了，检索词往往也要跟着变）。不允许改 scene_id、"
                    "claim_ids、evidence_ids。旁白总时长有约 4.2 字/秒的预算约束。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string", "description": "要修改的分镜 scene_id。"},
                        "narration": {
                            "type": "string",
                            "description": "新旁白（中文，口播稿）。不传则保持不变。",
                        },
                        "visual_query_en": {
                            "type": "string",
                            "description": "新的英文视觉检索词（用于 B-roll 搜索）。不传则保持不变。",
                        },
                    },
                    "required": ["scene_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_title_hook",
                "description": "修改视频标题和/或开场 hook。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "新标题。不传则不变。"},
                        "hook": {"type": "string", "description": "新 hook。不传则不变。"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reorder_scenes",
                "description": (
                    "调整分镜顺序。scene_ids 必须是当前所有 scene_id 的一个排列"
                    "（不多不少、不增不减），否则会被拒绝。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "重排后的完整 scene_id 列表。",
                        },
                    },
                    "required": ["scene_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_scene",
                "description": (
                    "在指定位置插入一个新分镜。**只允许引用已有的 claim 与 evidence**（取当前分镜计划里"
                    "已经出现过的 claim_id / evidence_id），不允许新增 claim 或新证据——保证事实图不变。"
                    "用于 completeness / adequacy 低分时补一个承接或收束镜头;narration 用完整中文句。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_id": {
                            "type": "string",
                            "description": "新分镜的 scene_id（ASCII/数字/_/-，不与现有冲突）。",
                        },
                        "insert_after": {
                            "type": "string",
                            "description": "插到哪个分镜之后（其 scene_id）；用 'start' 表示插到最前。",
                        },
                        "narration": {"type": "string", "description": "新分镜旁白（完整中文口播句）。"},
                        "visual_query_en": {"type": "string", "description": "新分镜的英文视觉检索词。"},
                        "claim_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "引用的已有 claim_id（至少 1 个）。",
                        },
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "引用的已有 evidence_id（必须 ⊆ 这些 claim 的 evidence）。",
                        },
                    },
                    "required": ["scene_id", "narration", "visual_query_en", "claim_ids", "evidence_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remove_scene",
                "description": (
                    "删除一个冗余/重复分镜。约束:不能删到只剩 1 个分镜,不能删除其它分镜引用所需 claim 的"
                    "唯一承载分镜(会导致那些 claim 失去证据)。用于 flow/completeness 低分时去重。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string", "description": "要删除的分镜 scene_id。"},
                    },
                    "required": ["scene_id"],
                },
            },
        },
    ]


def _render_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "set_transition",
                "description": (
                    "给两个分镜之间的切点设置真正的视觉转场（xfade）。大部分切点"
                    "应保持硬切——只在情绪转折/场景切换的关键节拍上使用，用太多"
                    "会显得廉价。这是修复 transition 维度低分的主要手段。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_id": {
                            "type": "string",
                            "description": "转场后一镜的 scene_id（转场发生在它和上一镜之间）。",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["fade", "slideleft", "slideright", "wipeleft", "circleopen"],
                            "description": "fade 柔和；slideleft/slideright/wipeleft 有方向感；circleopen 最醒目。",
                        },
                        "duration_s": {
                            "type": "number",
                            "description": "转场时长（0.2-0.8 秒），默认 0.4。",
                            "default": 0.4,
                        },
                    },
                    "required": ["scene_id", "kind"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_emphasis_caption",
                "description": (
                    "给某个分镜加屏幕上方/中央的强调花字（与底部字幕并存的"
                    "drawtext 叠加层）。只在有真正值得强调的关键信息时用，"
                    "不是每个分镜都要加。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string", "description": "花字所在分镜 scene_id。"},
                        "text": {
                            "type": "string",
                            "description": "花字文本（≤24 字，越短越有力）。",
                        },
                        "position": {
                            "type": "string",
                            "enum": ["top", "center"],
                            "description": "top=屏幕上方（默认）；center=屏幕中央。",
                            "default": "top",
                        },
                        "start_frac": {
                            "type": "number",
                            "description": "出现时机，占该分镜时长的比例（0-1），默认 0。",
                            "default": 0.0,
                        },
                        "end_frac": {
                            "type": "number",
                            "description": "消失时机，占该分镜时长的比例（0-1），默认 1。",
                            "default": 1.0,
                        },
                    },
                    "required": ["scene_id", "text"],
                },
            },
        },
    ]


def _bgm_tool_schema(tracks: set[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "place_bgm",
            "description": "给整条视频选一条背景音乐（贯穿全片，低音量混在旁白下面）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "track": {
                        "type": "string",
                        "enum": sorted(tracks),
                        "description": "BGM 文件名（只能从可用曲目里选）。",
                    },
                    "volume": {
                        "type": "number",
                        "description": "音量（0-0.6），默认 0.15。旁白永远是主体。",
                        "default": 0.15,
                    },
                },
                "required": ["track"],
            },
        },
    }


def _sfx_tool_schema(tracks: set[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "place_sfx",
            "description": (
                "在某个分镜的指定时刻插入一个一次性音效（只响一下，如转场声、"
                "提示音）。不是背景音乐。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string", "description": "音效所在分镜 scene_id。"},
                    "track": {
                        "type": "string",
                        "enum": sorted(tracks),
                        "description": "音效文件名（只能从可用音效里选）。",
                    },
                    "offset_s": {
                        "type": "number",
                        "description": "相对该分镜开始的偏移秒数（0-10），默认 0。",
                        "default": 0.0,
                    },
                    "volume": {
                        "type": "number",
                        "description": "音量（0-1），默认 0.8。",
                        "default": 0.8,
                    },
                },
                "required": ["scene_id", "track"],
            },
        },
    }


def _material_tool_schemas(has_vl: bool) -> list[dict]:
    """B-roll selection tools. The agent owns which clip lands on each scene;
    this subsumes the pipeline's old fixed VL-reselect loop. All operate on
    in-session candidates — the agent sees material_ids, never raw URLs.
    rerank_broll is registered only when a VL rerank provider is configured,
    so the model can't ask for a capability that doesn't exist."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_broll",
                "description": (
                    "用英文检索词在 Pexels 搜索可授权 B-roll 候选（不会下载，也不改动分镜）。"
                    "用于为某个分镜找替换素材或有用的候选集；给 query 时尽量具体到可被拍到"
                    "的视觉主体。返回候选的 material_id、标题、时长、宽高比与 VL 预览分(如可用)。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_id": {
                            "type": "string",
                            "description": "这些候选打算给哪个分镜用（用于本轮候选登记与去重参考）。",
                        },
                        "query": {"type": "string", "description": "英文检索词（具体视觉主体）。"},
                    },
                    "required": ["scene_id", "query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "attach_broll",
                "description": (
                    "把某条已检索到的候选（material_id）指派为指定分镜的 B-roll，立即下载并落盘、"
                    "计算 SHA-256，换前如旧素材没被其它分镜复用则归档。用于 relevance 低分/"
                    "VL 提示与旁白不匹配时替换素材。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string", "description": "要替换/补充分镜 scene_id。"},
                        "material_id": {
                            "type": "string",
                            "description": "来自 search_broll / rerank_broll 结果的 material_id。",
                        },
                        "reason": {"type": "string", "description": "为什么换这条（人类可读，入 manifest）。"},
                    },
                    "required": ["scene_id", "material_id"],
                },
            },
        },
        (
            {
                "type": "function",
                "function": {
                    "name": "rerank_broll",
                    "description": (
                        "把 search_broll 找到的若干候选（或其全部）用 VL 按与指定分镜 narration 的"
                        "相关性重新排序，返回相关性从高到低的 material_id 与分数。挑素材前先 rerank。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "scene_id": {"type": "string", "description": "按哪个分镜的旁白打分。"},
                            "material_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "要排序的候选 material_id 列表；不传则对当前 session 里该 scene 已登记候选全部排序。",
                            },
                        },
                        "required": ["scene_id"],
                    },
                },
            }
            if has_vl
            else {
                "type": "function",
                "function": {
                    "name": "rerank_broll",
                    "description": "VL rerank 不可用（未配置 VL provider）。选一个候选时直接 attach_broll。",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ),
    ]


def _meta_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_scene_media",
                "description": (
                    "查看当前每个分镜已指派的素材(material_id/时长/标题/是否已下载)。"
                    "只读自查,任何时候都可用(即使未配置素材源)。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_storyboard",
                "description": (
                    "查看当前分镜（含已应用的内容修改）和已做出的渲染决策摘要，"
                    "用于自查有没有遗漏或冲突。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "确认本轮修改已经完成，结束。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "一句话总结本轮做了什么。"},
                    },
                },
            },
        },
    ]


def build_tool_schemas(
    pexels_source=None, vl_rerank_provider=None
) -> tuple[list[dict], set[str], set[str], bool, bool]:
    """(schemas, bgm_tracks, sfx_tracks, has_media, has_vl). Audio tools are
    registered only when their asset dirs yield at least one track; material
    tools only when a Pexels source is wired; rerank_broll is real only when a
    VL rerank provider is available."""
    bgm_tracks = available_tracks(BGM_DIR_ENV)
    sfx_tracks = available_tracks(SFX_DIR_ENV)
    has_media = pexels_source is not None
    has_vl = vl_rerank_provider is not None
    schemas = _content_tool_schemas() + _render_tool_schemas()
    if has_media:
        schemas += _material_tool_schemas(has_vl)
    if bgm_tracks:
        schemas.append(_bgm_tool_schema(bgm_tracks))
    if sfx_tracks:
        schemas.append(_sfx_tool_schema(sfx_tracks))
    schemas += _meta_tool_schemas()
    return schemas, bgm_tracks, sfx_tracks, has_media, has_vl


@dataclass
class EditingSession:
    """Mutable state for one agent run: the working plan + accumulated
    EditPlan + a log of applied edits (for the manifest's provenance trail)."""

    plan: VideoPlan
    max_narration_chars: int
    bgm_tracks: set[str] = field(default_factory=set)
    sfx_tracks: set[str] = field(default_factory=set)
    # Scene count floor/cap so the editing agent can restructure (add/remove
    # scenes) without the plan collapsing into a fragment or ballooning past the
    # duration budget. Carried in from the pipeline so single-shot & tool-loop
    # paths share the same guard.
    min_scenes: int = 2
    max_scenes: int = 12
    edit_plan: EditPlan = field(default_factory=EditPlan)
    applied: list[str] = field(default_factory=list)
    # B-roll selection context: wired only when the pipeline passes a Pexels
    # source (and optionally a VL rerank provider). Candidates the agent finds
    # are registered here keyed by material_id so attach_broll can download the
    # right Material object. used_material_ids tracks cross-scene dedupe.
    pexels_source: object | None = None
    vl_rerank_provider: object | None = None
    _candidates: dict[str, Material] = field(default_factory=dict)
    _used_material_ids: set[str] = field(default_factory=set)

    @classmethod
    def for_plan(
        cls,
        plan: VideoPlan,
        *,
        bgm_tracks: set[str] | None = None,
        sfx_tracks: set[str] | None = None,
        pexels_source=None,
        vl_rerank_provider=None,
    ) -> "EditingSession":
        # Same spoken-time budget the rubric editor used: ~4.2 chars/s, +15%.
        target_chars = max(20, int(plan.estimated_duration_s * 4.2))
        return cls(
            plan=plan,
            max_narration_chars=int(target_chars * 1.15),
            bgm_tracks=bgm_tracks or set(),
            sfx_tracks=sfx_tracks or set(),
            pexels_source=pexels_source,
            vl_rerank_provider=vl_rerank_provider,
        )

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        """Execute one tool call. Returns (result_text_for_model, is_finish).
        Never raises — bad input becomes an error string the model can read."""
        try:
            if name == "edit_scene":
                return self._edit_scene(args), False
            if name == "edit_title_hook":
                return self._edit_title_hook(args), False
            if name == "reorder_scenes":
                return self._reorder_scenes(args), False
            if name == "add_scene":
                return self._add_scene(args), False
            if name == "remove_scene":
                return self._remove_scene(args), False
            if name == "set_transition":
                return self._set_transition(args), False
            if name == "set_emphasis_caption":
                return self._set_emphasis_caption(args), False
            if name == "search_broll":
                return self._search_broll(args), False
            if name == "attach_broll":
                return self._attach_broll(args), False
            if name == "rerank_broll":
                return self._rerank_broll(args), False
            if name == "get_scene_media":
                return self._get_scene_media(), False
            # get_scene_media is read-only inspection: registered even without a
            # Pexels source so the model can always self-check what's attached.
            if name == "place_bgm":
                return self._place_bgm(args), False
            if name == "place_sfx":
                return self._place_sfx(args), False
            if name == "get_storyboard":
                return self._storyboard(), False
            if name == "finish":
                summary = str(args.get("summary", "")).strip()
                return (f"已结束。{summary}" if summary else "已结束。"), True
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False), False
        except Exception as exc:  # pydantic ValidationError and friends
            return json.dumps({"error": f"参数有误: {str(exc)[:200]}"}, ensure_ascii=False), False

    # -- content edits (guarded) -------------------------------------------

    def _apply_guarded(self, mutate, label: str) -> str:
        """Apply `mutate(candidate)` to a deep copy, validate, commit or reject.

        Used by every plan-mutating tool (content edits AND attach_broll). A
        rejected mutate leaves self.plan untouched; the tool result text tells
        the model why so it can retry within its turn budget."""
        candidate = self.plan.model_copy(deep=True)
        try:
            mutate(candidate)
            candidate = VideoPlan.model_validate(candidate.model_dump())
        except (ValidationError, ValueError, KeyError) as exc:
            return json.dumps({"error": f"修改未通过校验，未生效: {str(exc)[:200]}"},
                              ensure_ascii=False)
        reason = check_soft_edit_invariants(
            self.plan, candidate, max_chars=self.max_narration_chars
        )
        if reason is not None:
            return json.dumps({"error": f"修改被拒绝，未生效: {reason}"}, ensure_ascii=False)
        self.plan = candidate
        self.applied.append(label)
        return json.dumps({"ok": True, "applied": label}, ensure_ascii=False)

    def _edit_scene(self, args: dict) -> str:
        scene_id = str(args.get("scene_id", ""))
        narration = args.get("narration")
        visual_query = args.get("visual_query_en")
        if narration is None and visual_query is None:
            return json.dumps({"error": "narration 和 visual_query_en 至少给一个"},
                              ensure_ascii=False)
        if scene_id not in {s.scene_id for s in self.plan.scenes}:
            return json.dumps({"error": f"未知 scene_id: {scene_id}"}, ensure_ascii=False)

        def mutate(plan: VideoPlan) -> None:
            for scene in plan.scenes:
                if scene.scene_id == scene_id:
                    if narration is not None:
                        scene.narration = str(narration)
                    if visual_query is not None:
                        scene.visual_query_en = str(visual_query)

        return self._apply_guarded(mutate, f"edit_scene({scene_id})")

    def _edit_title_hook(self, args: dict) -> str:
        title = args.get("title")
        hook = args.get("hook")
        if title is None and hook is None:
            return json.dumps({"error": "title 和 hook 至少给一个"}, ensure_ascii=False)

        def mutate(plan: VideoPlan) -> None:
            if title is not None:
                plan.title = str(title)
            if hook is not None:
                plan.hook = str(hook)

        return self._apply_guarded(mutate, "edit_title_hook")

    def _reorder_scenes(self, args: dict) -> str:
        order = [str(s) for s in (args.get("scene_ids") or [])]
        current = [s.scene_id for s in self.plan.scenes]
        if sorted(order) != sorted(current):
            return json.dumps(
                {"error": f"scene_ids 必须是当前分镜的排列，当前为: {current}"},
                ensure_ascii=False,
            )

        def mutate(plan: VideoPlan) -> None:
            by_id = {s.scene_id: s for s in plan.scenes}
            plan.scenes = [by_id[sid] for sid in order]

        return self._apply_guarded(mutate, f"reorder_scenes({order})")

    def _add_scene(self, args: dict) -> str:
        if len(self.plan.scenes) >= self.max_scenes:
            return json.dumps({"error": f"已达分镜上限 {self.max_scenes}"}, ensure_ascii=False)
        scene_id = str(args.get("scene_id", "")).strip()
        if not scene_id:
            return json.dumps({"error": "scene_id 不能为空"}, ensure_ascii=False)
        if scene_id in {s.scene_id for s in self.plan.scenes}:
            return json.dumps({"error": f"scene_id 已存在: {scene_id}"}, ensure_ascii=False)
        claim_ids = [str(c) for c in (args.get("claim_ids") or [])]
        evidence_ids = [str(e) for e in (args.get("evidence_ids") or [])]
        # Structural hard rule: a new scene may only cite claims/evidence that
        # already exist in the plan — the agent restructures, it must not extend
        # the factual base (that's the evidence collector's / planner's job).
        existing_claims = {c.claim_id for c in self.plan.claims}
        existing_evidence = {e for c in self.plan.claims for e in c.evidence_ids}
        unknown_claims = set(claim_ids) - existing_claims
        unknown_evidence = set(evidence_ids) - existing_evidence
        if unknown_claims:
            return json.dumps({"error": f"新分镜引用了不存在的 claim: {sorted(unknown_claims)}"},
                              ensure_ascii=False)
        if unknown_evidence:
            return json.dumps({"error": f"新分镜引用了不存在的 evidence: {sorted(unknown_evidence)}"},
                              ensure_ascii=False)
        insert_after = str(args.get("insert_after", "") or "end")
        current_order = [s.scene_id for s in self.plan.scenes]
        if insert_after not in current_order and insert_after not in ("start", "end"):
            return json.dumps({"error": f"insert_after 未知分镜或哨兵: {insert_after}"},
                              ensure_ascii=False)

        def mutate(plan: VideoPlan) -> None:
            new_scene = Scene(
                scene_id=scene_id,
                narration=str(args.get("narration", "")),
                visual_query_en=str(args.get("visual_query_en", "")),
                claim_ids=claim_ids,
                evidence_ids=evidence_ids,
            )
            if insert_after == "start":
                plan.scenes.insert(0, new_scene)
            elif insert_after == "end":
                plan.scenes.append(new_scene)
            else:
                idx = current_order.index(insert_after)
                plan.scenes.insert(idx + 1, new_scene)

        return self._apply_guarded(mutate, f"add_scene({scene_id})")

    def _remove_scene(self, args: dict) -> str:
        if len(self.plan.scenes) <= self.min_scenes:
            return json.dumps({"error": f"不能低于分镜下限 {self.min_scenes}"}, ensure_ascii=False)
        scene_id = str(args.get("scene_id", ""))
        if scene_id not in {s.scene_id for s in self.plan.scenes}:
            return json.dumps({"error": f"未知 scene_id: {scene_id}"}, ensure_ascii=False)

        # If this scene is the only one citing some claim, removing it leaves
        # that claim attached to no scene — still schema-valid, but the claim
        # silently drops out of the video. Surface it as a note the model sees.
        losing_claims = [
            cid
            for cid in {cid for s in self.plan.scenes if s.scene_id == scene_id for cid in s.claim_ids}
            if all(cid not in s.claim_ids for s in self.plan.scenes if s.scene_id != scene_id)
        ]

        def mutate(plan: VideoPlan) -> None:
            plan.scenes = [s for s in plan.scenes if s.scene_id != scene_id]

        label = f"remove_scene({scene_id})"
        n_before = len(self.plan.scenes)
        result = self._apply_guarded(mutate, label)
        if len(self.plan.scenes) < n_before and losing_claims:
            # Edit applied: append the orphan-claim note so the model knows.
            payload = json.loads(result)
            if payload.get("ok"):
                payload["note"] = f"这些 claim 已无场景引用,视频里不再出现: {losing_claims}"
                result = json.dumps(payload, ensure_ascii=False)
        return result

    # -- render decisions ---------------------------------------------------

    def _known_scene(self, scene_id: str) -> str | None:
        if scene_id not in {s.scene_id for s in self.plan.scenes}:
            return json.dumps({"error": f"未知 scene_id: {scene_id}"}, ensure_ascii=False)
        if scene_id == self.plan.scenes[0].scene_id:
            return json.dumps(
                {"error": "第一个分镜之前没有切点，转场请从第二个分镜起设置"},
                ensure_ascii=False,
            )
        return None

    def _set_transition(self, args: dict) -> str:
        spec = TransitionSpec.model_validate(args)
        if err := self._known_scene(spec.scene_id):
            return err
        self.edit_plan.transitions = [
            t for t in self.edit_plan.transitions if t.scene_id != spec.scene_id
        ]
        self.edit_plan.transitions.append(spec)
        self.applied.append(f"set_transition({spec.scene_id}<-{spec.kind})")
        return json.dumps({"ok": True}, ensure_ascii=False)

    def _set_emphasis_caption(self, args: dict) -> str:
        spec = EmphasisCaption.model_validate(args)
        if spec.scene_id not in {s.scene_id for s in self.plan.scenes}:
            return json.dumps({"error": f"未知 scene_id: {spec.scene_id}"}, ensure_ascii=False)
        if spec.end_frac <= spec.start_frac:
            return json.dumps({"error": "end_frac 必须大于 start_frac"}, ensure_ascii=False)
        self.edit_plan.captions = [
            c for c in self.edit_plan.captions if c.scene_id != spec.scene_id
        ]
        self.edit_plan.captions.append(spec)
        self.applied.append(f"set_emphasis_caption({spec.scene_id}={spec.text!r})")
        return json.dumps({"ok": True}, ensure_ascii=False)

    # -- material selection (agent-owned B-roll, subsumes the old VL loop) -----

    def _media_error(self) -> str | None:
        if self.pexels_source is None:
            return json.dumps({"error": "未配置 Pexels 素材源，B-roll 工具不可用"}, ensure_ascii=False)
        return None

    def _search_broll(self, args: dict) -> str:
        if err := self._media_error():
            return err
        scene_id = str(args.get("scene_id", ""))
        if scene_id not in {s.scene_id for s in self.plan.scenes}:
            return json.dumps({"error": f"未知 scene_id: {scene_id}"}, ensure_ascii=False)
        query = str(args.get("query", "")).strip()
        if len(query) < 2:
            return json.dumps({"error": "query 至少 2 个字符"}, ensure_ascii=False)
        try:
            candidates = self.pexels_source.search(query, per_page=6)
        except Exception as exc:  # network/gateway — not the model's fault
            return json.dumps({"error": f"搜索失败: {type(exc).__name__}: {str(exc)[:120]}"},
                              ensure_ascii=False)
        if not candidates:
            return json.dumps({"query": query, "candidates": [], "note": "没有命中"}, ensure_ascii=False)
        out = []
        for cand in candidates:
            self._candidates[cand.material_id] = cand
            out.append(
                {
                    "material_id": cand.material_id,
                    "title": cand.title,
                    "author": cand.author,
                    "duration_s": cand.duration_s,
                    "width": cand.width,
                    "height": cand.height,
                    "already_used_elsewhere": cand.material_id in self._used_material_ids,
                }
            )
        return json.dumps({"query": query, "scene_id": scene_id, "candidates": out}, ensure_ascii=False)

    def _attach_broll(self, args: dict) -> str:
        if err := self._media_error():
            return err
        scene_id = str(args.get("scene_id", ""))
        target = next((s for s in self.plan.scenes if s.scene_id == scene_id), None)
        if target is None:
            return json.dumps({"error": f"未知 scene_id: {scene_id}"}, ensure_ascii=False)
        material_id = str(args.get("material_id", ""))
        cand = self._candidates.get(material_id)
        if cand is None:
            return json.dumps(
                {"error": f"未知 material_id: {material_id}。先 search_broll/rerank_broll 再 attach。"},
                ensure_ascii=False,
            )
        # Cross-scene dedupe: this material is committed to a different scene.
        holder = next(
            (s.scene_id for s in self.plan.scenes if s.scene_id != scene_id
             and s.material and s.material.material_id == material_id),
            None,
        )
        if holder:
            return json.dumps({"error": f"该素材已用于分镜 {holder}，换一条避免重复"}, ensure_ascii=False)
        # Release the material this scene was holding so a sibling could swap to it.
        old_id = target.material.material_id if target.material else None
        if old_id and old_id != material_id:
            self._used_material_ids.discard(old_id)
        try:
            downloaded = self.pexels_source.download(cand)
        except Exception as exc:
            return json.dumps({"error": f"下载失败，未替换: {type(exc).__name__}: {str(exc)[:120]}"},
                              ensure_ascii=False)

        def mutate(plan: VideoPlan) -> None:
            for s in plan.scenes:
                if s.scene_id == scene_id:
                    s.material = downloaded

        label = f"attach_broll({scene_id}<-{material_id})"
        n_before = next((s.material.material_id if s.material else None
                        for s in self.plan.scenes if s.scene_id == scene_id), None)
        result = self._apply_guarded(mutate, label)
        current = next((s.material for s in self.plan.scenes if s.scene_id == scene_id), None)
        if current and current.material_id == material_id and n_before != material_id:
            self._used_material_ids.add(material_id)
        reason = str(args.get("reason", "")).strip()
        if reason and '"ok": true' in result:
            self.applied.append(f"reason: {reason}")
        return result

    def _rerank_broll(self, args: dict) -> str:
        if self.vl_rerank_provider is None:
            return json.dumps({"error": "VL rerank 不可用（未配置 VL provider）"}, ensure_ascii=False)
        scene_id = str(args.get("scene_id", ""))
        target = next((s for s in self.plan.scenes if s.scene_id == scene_id), None)
        if target is None:
            return json.dumps({"error": f"未知 scene_id: {scene_id}"}, ensure_ascii=False)
        ids = args.get("material_ids") or list(self._candidates.keys())
        cands = [self._candidates[i] for i in ids if i in self._candidates]
        if not cands:
            return json.dumps({"error": "没有可排序的候选，先 search_broll。"}, ensure_ascii=False)
        from tracecut.vl_judge import rerank_candidates  # local: heavy import (ffmpeg chain)

        ranked = rerank_candidates(self.vl_rerank_provider, target.narration, cands)
        top = [
            {"material_id": cand.material_id, "relevance": score, "title": cand.title}
            for cand, score in ranked
        ]
        return json.dumps({"scene_id": scene_id, "ranked": top}, ensure_ascii=False)

    def _get_scene_media(self) -> str:
        rows = [
            {
                "scene_id": s.scene_id,
                "material_id": s.material.material_id if s.material else None,
                "title": s.material.title if s.material else None,
                "duration_s": s.material.duration_s if s.material else None,
                "downloaded": bool(s.material and s.material.local_path),
            }
            for s in self.plan.scenes
        ]
        return json.dumps(rows, ensure_ascii=False)

    def _place_bgm(self, args: dict) -> str:
        if not self.bgm_tracks:
            return json.dumps({"error": "BGM 资产目录未配置，此能力不可用"}, ensure_ascii=False)
        spec = BgmSpec.model_validate(args)
        if spec.track not in self.bgm_tracks:
            return json.dumps(
                {"error": f"未知曲目 {spec.track!r}，可用: {sorted(self.bgm_tracks)}"},
                ensure_ascii=False,
            )
        self.edit_plan.bgm = spec
        self.applied.append(f"place_bgm({spec.track}@{spec.volume})")
        return json.dumps({"ok": True}, ensure_ascii=False)

    def _place_sfx(self, args: dict) -> str:
        if not self.sfx_tracks:
            return json.dumps({"error": "SFX 资产目录未配置，此能力不可用"}, ensure_ascii=False)
        spec = SfxSpec.model_validate(args)
        if spec.track not in self.sfx_tracks:
            return json.dumps(
                {"error": f"未知音效 {spec.track!r}，可用: {sorted(self.sfx_tracks)}"},
                ensure_ascii=False,
            )
        if spec.scene_id not in {s.scene_id for s in self.plan.scenes}:
            return json.dumps({"error": f"未知 scene_id: {spec.scene_id}"}, ensure_ascii=False)
        self.edit_plan.sfx.append(spec)
        self.applied.append(f"place_sfx({spec.scene_id}+{spec.offset_s}s={spec.track})")
        return json.dumps({"ok": True}, ensure_ascii=False)

    def _storyboard(self) -> str:
        return json.dumps(
            {
                "plan": json.loads(plan_digest(self.plan)),
                "render_decisions": self.edit_plan.summary(),
                "applied_edits": self.applied,
            },
            ensure_ascii=False,
        )
