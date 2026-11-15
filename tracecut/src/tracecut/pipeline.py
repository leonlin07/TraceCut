from __future__ import annotations

import json
import re
import tempfile
import uuid
from pathlib import Path

from tracecut.config import Settings
from tracecut.editing.loop import run_editing_agent
from tracecut.evaluate import deterministic_evaluation, review_plan
from tracecut.media import render_video
from tracecut.planner import make_plan
from tracecut.providers.llm import make_provider, make_vl_provider
from tracecut.publish import generate_publish_meta, write_publish_json
from tracecut.rubric import IterationConfig, RubricReport, iterate_with_rubric
from tracecut.schemas import EvidenceDoc, RunManifest, VideoPlan
from tracecut.sources.pexels import PexelsSource
from tracecut.sources.web import EvidenceCollector
from tracecut.tts import make_tts_backend
from tracecut.vl_judge import VLReport, judge_visual_relevance, rerank_candidates


class TraceCutPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(
        self,
        *,
        topic: str,
        source_urls: list[str],
        duration_s: int = 30,
        target_audience: str = "general",
        style: str = "factual-explainer",
        planner_provider: str = "siliconflow",
        reviewer_provider: str = "venus",
        download_media: bool = True,
        render: bool = False,
        rubric_iters: int = 0,
        run_vl_judge: bool = False,
        vl_loop: int = 0,
        director: bool = False,
        progress: "Callable[[str, float], None] | None" = None,
    ) -> Path:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self.settings.output_dir / f"{safe_slug(topic)}_{run_id}"
        run_dir.mkdir(parents=True, exist_ok=False)
        warnings: list[str] = []

        def _report(stage: str, percent: float) -> None:
            """Emit a coarse-grained stage progress signal, if a callback is set."""
            if progress is not None:
                progress(stage, percent)

        _report("start", 0.0)

        collector = EvidenceCollector(timeout_s=30)
        try:
            evidence = collector.collect(topic, source_urls)
        finally:
            collector.close()
        if not evidence:
            raise RuntimeError("No evidence was collected. Provide at least one accessible source URL.")
        _report("evidence", 15.0)

        planner = make_provider(self.settings, planner_provider)
        try:
            plan = make_plan(
                planner,
                topic=topic,
                evidence=evidence,
                duration_s=duration_s,
                target_audience=target_audience,
                style=style,
            )
        finally:
            planner.close()
        _report("plan", 35.0)

        if download_media and self.settings.pexels_api_key:
            self._attach_media(plan, run_dir, warnings)
        elif download_media:
            warnings.append("PEXELS_API_KEY is missing; scenes will render with title cards.")
        _report("media", 45.0)

        evaluation = deterministic_evaluation(plan, evidence)
        reviewer_used, reviewer_notes = self._run_reviewer(reviewer_provider, plan, evidence)
        evaluation.reviewer_notes = reviewer_notes
        _report("evaluate", 55.0)

        rubric_report = None
        if rubric_iters > 0:
            plan, rubric_report = self._run_rubric_iteration(
                plan, evidence, planner_provider, rubric_iters, warnings
            )
            # Iteration only edits soft variables, but re-evaluate structural coverage
            # against the final plan for a consistent manifest.
            evaluation = deterministic_evaluation(plan, evidence)
            evaluation.reviewer_notes = reviewer_notes
        _report("rubric", 65.0)

        # Director editing agent (T11): optional agentic polish after rubric.
        edit_plan_dict: dict | None = None
        if director and rubric_report is not None:
            edit_plan_dict = self._run_director(
                plan, evidence, rubric_report, run_dir, warnings
            )
        _report("director", 70.0)

        if evaluation.unsupported_claim_ids:
            warnings.append(
                "Broken claim-to-evidence references detected: "
                + ", ".join(evaluation.unsupported_claim_ids)
            )
            if render:
                warnings.append("Rendering was blocked because the provenance graph is incomplete.")
                render = False

        output_video = ""
        subtitle_path = ""
        cover_path_out = ""
        publish_meta = None
        vl_report = None
        status = "planned"
        if render:
            tts = self._make_tts(warnings)
            scene_durations: dict[str, float] = {}
            try:
                with tempfile.TemporaryDirectory(prefix="tracecut_render_") as temp_dir:
                    final_video, final_srt, scene_durations, cover_path = render_video(
                        plan,
                        run_dir / "final.mp4",
                        Path(temp_dir),
                        tts=tts,
                        bgm_dir=self.settings.bgm_dir if self.settings.bgm_dir.is_dir() else None,
                        bgm_llm=make_provider(self.settings, planner_provider) if self.settings.provider_ready(planner_provider) else None,
                        with_transitions=True,
                        generate_cover_flag=True,
                    )
                output_video = str(final_video)
                subtitle_path = str(final_srt)
                cover_path_out = str(cover_path) if cover_path and cover_path.exists() else ""

                # Generate Douyin publishing metadata
                try:
                    meta_provider = make_provider(self.settings, planner_provider)
                    try:
                        pub = generate_publish_meta(meta_provider, plan, topic)
                    finally:
                        meta_provider.close()
                    publish_path = run_dir / "publish.json"
                    write_publish_json(pub, final_video, Path(cover_path_out) if cover_path_out else Path(""), publish_path)
                    publish_meta = pub.to_dict()
                except Exception as exc:
                    warnings.append(f"publish meta generation failed: {type(exc).__name__}: {str(exc)[:120]}")
                    publish_meta = None
            finally:
                tts.close()
            status = "rendered"
            _report("render", 85.0)
            # VL judge + quality gate (T12). If run_vl_judge is set, we run the
            # visual judge and automatically reselect B-roll up to 2 rounds when
            # the average score is below 3 or any scene is below 2. If it still
            # fails, we mark the run as needing human review instead of aborting.
            did_vl_gate = run_vl_judge
            if run_vl_judge and scene_durations:
                vl_report = self._run_vl_judge(plan, final_video, scene_durations, run_dir, warnings)
                if vl_report is not None:
                    for attempt in range(2):
                        low = [s for s in vl_report.scores if s.relevance < 3]
                        min_score = min((s.relevance for s in vl_report.scores), default=5)
                        if vl_report.average >= 3.0 and min_score >= 2:
                            warnings.append(
                                f"VL quality gate passed (avg={vl_report.average:.2f}, min={min_score})"
                            )
                            break
                        warnings.append(
                            f"VL quality gate failed (avg={vl_report.average:.2f}, min={min_score}); "
                            f"reselect attempt {attempt + 1}/2"
                        )
                        vl_report = self._vl_reselect_loop(
                            plan, vl_report, run_dir, warnings, max_rounds=1
                        )
                        # Re-render with reselected media.
                        tts2 = self._make_tts(warnings)
                        try:
                            with tempfile.TemporaryDirectory(prefix="tracecut_render_") as temp_dir:
                                final_video, final_srt, scene_durations, cover_path = render_video(
                                    plan, run_dir / "final.mp4", Path(temp_dir), tts=tts2,
                                    bgm_dir=self.settings.bgm_dir if self.settings.bgm_dir.is_dir() else None,
                                    bgm_llm=make_provider(self.settings, planner_provider) if self.settings.provider_ready(planner_provider) else None,
                                    with_transitions=True,
                                    generate_cover_flag=True,
                                )
                            output_video = str(final_video)
                            subtitle_path = str(final_srt)
                            cover_path_out = str(cover_path) if cover_path and cover_path.exists() else ""
                        finally:
                            tts2.close()
                        vl_report = self._run_vl_judge(
                            plan, final_video, scene_durations, run_dir, warnings
                        ) or vl_report
                    else:
                        # Gate still failed after max attempts.
                        min_score = min((s.relevance for s in vl_report.scores), default=5)
                        if vl_report.average < 3.0 or min_score < 2:
                            warnings.append(
                                "VL quality gate: needs_human review after 2 reselect rounds"
                            )
            # Backward-compatible explicit VL loop — only when the `--vl` quality
            # gate did NOT already run (the gate already does up to 2 reselect
            # rounds; re-running reselect here duplicated the same queries and
            # wasted TTS/render passes when both flags were set).
            if vl_loop > 0 and not did_vl_gate and scene_durations:
                warnings.append(
                    "run_vl_judge=False but vl_loop>0; enabling VL judge for the loop."
                )
                vl_report = self._run_vl_judge(plan, final_video, scene_durations, run_dir, warnings)
            if vl_loop > 0 and not did_vl_gate and vl_report is not None:
                vl_report = self._vl_reselect_loop(
                    plan, vl_report, run_dir, warnings, max_rounds=vl_loop
                )
                # Re-render once more with the reselected media, then re-judge.
                tts2 = self._make_tts(warnings)
                try:
                    with tempfile.TemporaryDirectory(prefix="tracecut_render_") as temp_dir:
                        final_video, final_srt, scene_durations, cover_path = render_video(
                            plan, run_dir / "final.mp4", Path(temp_dir), tts=tts2,
                            bgm_dir=self.settings.bgm_dir if self.settings.bgm_dir.is_dir() else None,
                            bgm_llm=make_provider(self.settings, planner_provider) if self.settings.provider_ready(planner_provider) else None,
                            with_transitions=True,
                            generate_cover_flag=True,
                        )
                    output_video = str(final_video)
                    subtitle_path = str(final_srt)
                    cover_path_out = str(cover_path) if cover_path and cover_path.exists() else ""
                finally:
                    tts2.close()
                vl_report = self._run_vl_judge(plan, final_video, scene_durations, run_dir, warnings)

            # Final content-integrity pass: any scene the VL judge flagged as
            # misleading_visual (generic stock a viewer might mistake for the real
            # thing) gets a small "示意图" watermark. This requires one extra
            # render, so it only runs when there actually are misleading scenes.
            if render and run_vl_judge and vl_report is not None:
                misleading = {s.scene_id for s in vl_report.scores if s.misleading_visual}
                if misleading:
                    warnings.append(
                        f"Overlaying 示意图 watermark on misleading scenes: {sorted(misleading)}"
                    )
                    tts3 = self._make_tts(warnings)
                    try:
                        with tempfile.TemporaryDirectory(prefix="tracecut_render_") as temp_dir:
                            final_video, final_srt, scene_durations, cover_path = render_video(
                                plan, run_dir / "final.mp4", Path(temp_dir), tts=tts3,
                                bgm_dir=self.settings.bgm_dir if self.settings.bgm_dir.is_dir() else None,
                                bgm_llm=make_provider(self.settings, planner_provider) if self.settings.provider_ready(planner_provider) else None,
                                with_transitions=True,
                                generate_cover_flag=True,
                                misleading_scene_ids=misleading,
                            )
                        output_video = str(final_video)
                        subtitle_path = str(final_srt)
                        cover_path_out = str(cover_path) if cover_path and cover_path.exists() else ""
                    finally:
                        tts3.close()
        _report("vl", 95.0)

        manifest = RunManifest(
            run_id=run_id,
            topic=topic,
            status=status,
            planner_provider=planner_provider,
            reviewer_provider=reviewer_used,
            evidence=evidence,
            plan=plan,
            evaluation=evaluation,
            rubric=_serialize_rubric(rubric_report),
            vl=_serialize_vl(vl_report),
            output_video=output_video,
            subtitle_path=subtitle_path,
            cover_path=cover_path_out,
            publish_meta=publish_meta,
            edit_plan=edit_plan_dict,
            warnings=warnings,
        )
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (run_dir / "citations.json").write_text(
            json.dumps(
                [
                    {
                        "evidence_id": item.evidence_id,
                        "title": item.title,
                        "url": item.url,
                        "source_type": item.source_type,
                    }
                    for item in evidence
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _report("done", 100.0)
        return manifest_path

    def _attach_media(self, plan: VideoPlan, run_dir: Path, warnings: list[str]) -> None:
        source = PexelsSource(
            self.settings.pexels_api_key,
            cache_dir=run_dir / "materials",
            timeout_s=45,
        )
        # Prefer the most relevant B-roll per scene from the start: use the VL
        # model to rerank candidate preview posters against the narration before
        # downloading, instead of grabbing the first non-duplicate hit. This is
        # the same rerank used in the post-render reselect loop, applied earlier
        # so a low-relevance first-hit doesn't survive into the final cut.
        vl_provider = None
        try:
            vl_provider = make_vl_provider(self.settings)
        except ValueError:
            pass
        used_ids: set[str] = set()
        try:
            for scene in plan.scenes:
                # Retry ladder: exact query first, then a broader (first-2-words)
                # fallback; within each query, try every candidate until one
                # downloads (the old code tried only the first candidate, so a
                # single failed download left the scene with no B-roll at all).
                words = (scene.visual_query_en or "").split()
                queries = [scene.visual_query_en] if scene.visual_query_en else []
                if len(words) > 2:
                    fallback = " ".join(words[:2])
                    if fallback not in queries:
                        queries.append(fallback)
                acquired = False
                for query in queries:
                    try:
                        candidates = source.search(query, per_page=6)
                    except Exception as exc:
                        warnings.append(
                            f"Media search failed for {scene.scene_id}: "
                            f"{type(exc).__name__}: {str(exc)[:100]}"
                        )
                        continue
                    if not candidates:
                        continue
                    ordered = candidates
                    if vl_provider is not None and len(candidates) > 1:
                        try:
                            ordered = [
                                c for c, _ in rerank_candidates(
                                    vl_provider, scene.narration, candidates
                                )
                            ]
                        except Exception as exc:
                            warnings.append(
                                f"VL rerank failed for {scene.scene_id}: "
                                f"{type(exc).__name__}: {str(exc)[:100]}"
                            )
                    for cand in ordered:
                        if cand.material_id in used_ids:
                            continue
                        try:
                            scene.material = source.download(cand)
                            used_ids.add(cand.material_id)
                            acquired = True
                            break
                        except Exception as exc:
                            warnings.append(
                                f"Media download failed for {scene.scene_id} "
                                f"({cand.material_id}): {type(exc).__name__}: {str(exc)[:100]}"
                            )
                    if acquired:
                        break
                if not acquired:
                    warnings.append(
                        f"No Pexels material for scene {scene.scene_id}; will render as a text card."
                    )
        finally:
            source.close()
            if vl_provider is not None:
                vl_provider.close()

    def _run_rubric_iteration(
        self,
        plan: VideoPlan,
        evidence: list[EvidenceDoc],
        planner_provider: str,
        rubric_iters: int,
        warnings: list[str],
    ) -> tuple[VideoPlan, RubricReport]:
        """Score the plan on a perceptual rubric and self-iterate on soft variables.

        The judge runs on SiliconFlow (same gateway as the planner). Edits are
        constrained to narration/visual_query/order; the provenance graph invariant
        is re-validated inside the rubric loop, so fidelity cannot drift.
        """
        editor = make_provider(self.settings, planner_provider)
        judge = make_provider(self.settings, "siliconflow")
        try:
            result = iterate_with_rubric(
                editor,
                judge,
                plan,
                evidence,
                IterationConfig(max_iters=rubric_iters),
            )
        finally:
            editor.close()
            judge.close()
        if result.history and result.history[-1].edits:
            warnings.extend(f"rubric: {e}" for e in result.history[-1].edits)
        return result.plan, result.report

    def _run_director(
        self,
        plan: VideoPlan,
        evidence: list[EvidenceDoc],
        rubric_report: RubricReport,
        run_dir: Path,
        warnings: list[str],
    ) -> dict | None:
        """Run the Director editing agent to make render-layer decisions.

        The agent can edit soft content variables and produce an EditPlan with
        transitions, emphasis captions, BGM and SFX choices. For now the EditPlan
        is stored in the manifest; media.py applies transitions/BGM defaults
        automatically even when the Director is not used.
        """
        try:
            pexels_source = None
            if self.settings.pexels_api_key:
                pexels_source = PexelsSource(
                    self.settings.pexels_api_key,
                    cache_dir=run_dir / "materials",
                    timeout_s=45,
                )
            vl_provider = None
            try:
                vl_provider = make_vl_provider(self.settings)
            except ValueError:
                pass
            provider = make_provider(self.settings, "siliconflow")
            try:
                result = run_editing_agent(
                    provider,
                    plan,
                    evidence,
                    rubric_report,
                    threshold=4,
                    pexels_source=pexels_source,
                    vl_rerank_provider=vl_provider,
                )
            finally:
                provider.close()
                if pexels_source:
                    pexels_source.close()
                if vl_provider:
                    vl_provider.close()
            if result.degraded:
                warnings.append(f"Director degraded: {result.degraded}")
            if result.applied:
                warnings.append(f"Director applied: {', '.join(result.applied)}")
            return result.edit_plan.model_dump(mode="json") if result.edit_plan else None
        except Exception as exc:
            warnings.append(f"Director agent failed: {type(exc).__name__}: {str(exc)[:120]}")
            return None

    def _run_vl_judge(
        self,
        plan: VideoPlan,
        video_path,
        scene_durations: dict[str, float],
        run_dir,
        warnings: list[str],
    ) -> VLReport | None:
        """Run the VL visual-relevance judge on rendered frames."""
        try:
            vl_provider = make_vl_provider(self.settings)
        except ValueError as exc:
            warnings.append(f"VL judge skipped: {str(exc)[:120]}")
            return None
        try:
            return judge_visual_relevance(
                vl_provider,
                plan,
                Path(video_path),
                run_dir / "vl_frames",
                scene_durations,
            )
        except Exception as exc:
            warnings.append(f"VL judge failed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
        finally:
            vl_provider.close()

    def _vl_reselect_loop(
        self,
        plan: VideoPlan,
        vl_report: VLReport,
        run_dir,
        warnings: list[str],
        *,
        max_rounds: int,
        threshold: int = 4,
    ) -> VLReport:
        """For each scene whose VL relevance < threshold, re-pick B-roll:
        re-search Pexels with an LLM-rewritten visual query, VL-rerank the
        candidates by poster-vs-narration, download the best. Repeat up to
        max_rounds. Returns the (possibly updated) VL report."""
        if not self.settings.pexels_api_key:
            warnings.append("VL reselect loop skipped: no PEXELS_API_KEY.")
            return vl_report
        low_ids = {s.scene_id for s in vl_report.scores if s.relevance < threshold}
        if not low_ids:
            warnings.append("VL reselect: no low-relevance scenes; nothing to fix.")
            return vl_report
        llm = make_provider(self.settings, "siliconflow")
        vl_provider = make_vl_provider(self.settings)
        source = PexelsSource(
            self.settings.pexels_api_key, cache_dir=run_dir / "materials", timeout_s=45
        )
        tried: dict[str, set[str]] = {}
        try:
            for _ in range(max_rounds):
                fixed_any = False
                for scene in plan.scenes:
                    if scene.scene_id not in low_ids:
                        continue
                    seen = tried.setdefault(scene.scene_id, set())
                    new_query = self._rewrite_visual_query(llm, scene, avoid=sorted(seen))
                    # Skip queries we already searched for this scene — the model
                    # otherwise keeps emitting the same rewrite round after round.
                    if not new_query or new_query in seen:
                        continue
                    seen.add(new_query)
                    try:
                        candidates = source.search(new_query, per_page=6)
                    except Exception as exc:
                        warnings.append(f"VL reselect search failed {scene.scene_id}: {str(exc)[:80]}")
                        continue
                    if not candidates:
                        continue
                    ranked = rerank_candidates(vl_provider, scene.narration, candidates)
                    if not ranked:
                        continue
                    best, score = ranked[0]
                    # Only swap if the rerank top score beats the current scene's
                    # VL relevance; otherwise keep what we have.
                    current = next((s for s in vl_report.scores if s.scene_id == scene.scene_id), None)
                    if current is not None and score <= current.relevance:
                        continue
                    try:
                        scene.material = source.download(best)
                        fixed_any = True
                        warnings.append(
                            f"VL reselect {scene.scene_id}: swapped to higher-relevance B-roll "
                            f"(query='{new_query[:30]}', rerank={score})"
                        )
                    except Exception as exc:
                        warnings.append(f"VL reselect download failed {scene.scene_id}: {str(exc)[:80]}")
                if not fixed_any:
                    break
        finally:
            llm.close()
            vl_provider.close()
            source.close()
        return vl_report

    def _rewrite_visual_query(self, provider, scene, avoid: list[str] | None = None) -> str:
        """LLM-rewrite a scene's visual_query_en to be more specific/concrete,
        to surface better Pexels candidates after a low VL relevance score."""
        import json as _json
        prompt = {
            "task": "Rewrite this stock-footage search query to be more specific and concrete.",
            "narration": scene.narration,
            "current_query": scene.visual_query_en,
            "avoid": avoid or [],
            "instruction": (
                'Return JSON: {"query": "a 3-6 word English search query"}. '
                "Make it concrete nouns a stock-footage search can match; avoid abstract words. "
                "Prefer REAL footage (photography/documentary) over CGI, renders, or abstract "
                "animations, so the visual does not get mistaken for a real scientific image. "
                "Do NOT reuse any query listed in 'avoid'."
            ),
        }
        raw = provider.complete_json(
            system="You rewrite stock-footage search queries. Return ONLY JSON.",
            user=_json.dumps(prompt, ensure_ascii=False),
            max_tokens=120,
        )
        q = str(raw.get("query", "")).strip()
        return q or scene.visual_query_en

    def _make_tts(self, warnings: list[str]):
        """Build the TTS backend. CosyVoice2 (SiliconFlow API) is the default and
        needs no GPU; if it is selected but the SiliconFlow key is missing, fall
        back to macOS ``say`` with a warning rather than aborting the render."""
        backend = self.settings.tts_backend
        if backend == "cosyvoice" and not self.settings.siliconflow_api_key:
            warnings.append(
                "TRACECUT_TTS=cosyvoice but SILICONFLOW_API_KEY is missing; "
                "falling back to macOS say."
            )
            backend = "say"
        return make_tts_backend(
            backend=backend,
            siliconflow_api_key=self.settings.siliconflow_api_key,
            siliconflow_base_url=self.settings.siliconflow_base_url,
            voice=self.settings.tts_voice,
            cosyvoice_voice=self.settings.cosyvoice_voice,
            tts_instruction=self.settings.tts_instruction,
        )

    def _run_reviewer(
        self,
        reviewer_provider: str,
        plan: VideoPlan,
        evidence: list[EvidenceDoc],
    ) -> tuple[str, list[str]]:
        """Always run the reviewer. Venus is the preferred gateway; if it is not
        configured or its call fails, fall back to SiliconFlow (the same base model
        via the external gateway). Never degrade to "deterministic audit only" — a
        reviewer that cannot run is a hard error, surfaced to the caller.
        """
        # Provider preference: the requested reviewer first, then SiliconFlow as the
        # guaranteed-available fallback (it is the planner's default gateway).
        candidates: list[str] = []
        if reviewer_provider and reviewer_provider != "siliconflow":
            candidates.append(reviewer_provider)
        candidates.append("siliconflow")
        # Dedupe while preserving order.
        seen: set[str] = set()
        ordered = [p for p in candidates if not (p in seen or seen.add(p))]

        errors: list[str] = []
        for name in ordered:
            if not self.settings.provider_ready(name):
                errors.append(f"{name}: not configured")
                continue
            provider = make_provider(self.settings, name)
            try:
                notes = review_plan(provider, plan, evidence)
                return name, notes
            except Exception as exc:  # provider reachable but call failed; try next
                errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
            finally:
                provider.close()
        raise RuntimeError("Reviewer failed on all providers: " + "; ".join(errors))


def safe_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text).strip("-")
    return (slug or "run")[:48]


def _serialize_rubric(report: RubricReport | None) -> dict | None:
    if report is None:
        return None
    return {
        "average": round(report.average, 3),
        "iterations": report.iterations,
        "scores": {
            dim: {"score": s.score, "note": s.note} for dim, s in report.scores.items()
        },
        "suggestions": report.suggestions,
        "edits": report.edits,
    }


def _serialize_vl(report: VLReport | None) -> dict | None:
    if report is None:
        return None
    return report.to_dict()

