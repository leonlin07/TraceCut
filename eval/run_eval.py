"""评测入口 + 消融实验。

用法：
  # 跑指定题（不渲染，快速验证评测逻辑）
  python -m eval.run_eval --ids inference-opt-v1 multimodal-retrieval-v1

  # 跑全部 20 题（含渲染 + VL 评判 + 成本）
  python -m eval.run_eval --render --vl --vl-loop 1 --rubric-iters 1

  # 消融：baseline（无 Director/无 VL） vs full（Director + VL）
  python -m eval.run_eval --ablation --ids inference-opt-v1 multimodal-retrieval-v1 ai-alignment-v1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cost.tracker import summarize  # noqa: E402
from eval.dataset.topics import CASES, EvalCase  # noqa: E402
from eval.metrics import aggregate_metrics, score_faithfulness  # noqa: E402
from eval.report import render_report  # noqa: E402
from tracecut.config import Settings  # noqa: E402
from tracecut.pipeline import TraceCutPipeline  # noqa: E402
from tracecut.providers.llm import make_provider, set_usage_sink  # noqa: E402


def run_case(
    settings: Settings,
    provider,
    case: EvalCase,
    *,
    render: bool,
    rubric_iters: int,
    run_vl_judge: bool,
    vl_loop: int,
    director: bool,
    usage: list[dict],
) -> dict[str, Any]:
    """跑一个 case，读 manifest，评测，返回聚合指标（含端到端耗时）。

    单个 case 失败不抛异常，而是返回带 ``error`` 字段的失败结果，避免一个
    偶发错误（如 LLM 输出解析失败）中断整组消融实验。
    """
    try:
        pipeline = TraceCutPipeline(settings)
        t0 = time.perf_counter()

        def on_progress(stage: str, percent: float) -> None:
            print(f"      [{stage}] {percent:.0f}%", flush=True)

        manifest_path = pipeline.run(
            topic=case.topic,
            source_urls=case.source_urls,
            duration_s=case.duration_s,
            render=render,
            rubric_iters=rubric_iters,
            run_vl_judge=run_vl_judge,
            vl_loop=vl_loop,
            director=director,
            progress=on_progress,
        )
        elapsed_s = round(time.perf_counter() - t0, 1)
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        # 忠实度评测单独容错：LLM 偶发连接失败不应丢弃已算出的 coverage/VL 指标
        try:
            faithfulness = score_faithfulness(provider, case, manifest)
        except Exception as exc:  # noqa: BLE001
            print(f"      [faithfulness failed] {type(exc).__name__}", flush=True)
            faithfulness = None
        result = aggregate_metrics(case, manifest, faithfulness)
        result["elapsed_s"] = elapsed_s
        # TTS 按字符计费，字数从 manifest 旁白汇总（与 serve/worker.py 口径一致）
        plan = manifest.get("plan", {})
        tts_chars = len(plan.get("hook", "") or "")
        for scene in plan.get("scenes", []):
            tts_chars += len(scene.get("narration", "") or "")
        result["tts_chars"] = tts_chars
        return result
    except Exception as exc:  # noqa: BLE001 - 容错，不中断整组
        print(f"      [FAILED] {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        return {
            "case_id": case.id,
            "topic": case.topic,
            "difficulty": case.difficulty,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "claim_provenance_coverage": None,
            "scene_evidence_coverage": None,
            "media_coverage": None,
            "faithfulness": None,
            "vl_relevance_avg": None,
            "misleading_ratio": None,
            "elapsed_s": None,
            "tts_chars": 0,
        }


def select_cases(ids: list[str] | None) -> list[EvalCase]:
    if not ids:
        return list(CASES)
    by_id = {c.id: c for c in CASES}
    return [by_id[i] for i in ids if i in by_id]


def _run_group(
    settings: Settings,
    provider,
    cases: list[EvalCase],
    *,
    label: str,
    render: bool,
    rubric_iters: int,
    run_vl_judge: bool,
    vl_loop: int,
    director: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    usage: list[dict] = []
    set_usage_sink(usage)
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            print(f"[{label}] {case.id}: {case.topic}")
            results.append(
                run_case(
                    settings, provider, case,
                    render=render, rubric_iters=rubric_iters,
                    run_vl_judge=run_vl_judge, vl_loop=vl_loop, director=director,
                    usage=usage,
                )
            )
    finally:
        set_usage_sink(None)
    total_tts = sum(r.get("tts_chars", 0) for r in results)
    cost = summarize(usage, tts_chars=total_tts).to_dict()
    return results, cost


def main() -> int:
    parser = argparse.ArgumentParser(description="TraceCut 评测入口")
    parser.add_argument("--ids", nargs="*", default=None, help="只跑指定 case id")
    parser.add_argument("--render", action="store_true", help="渲染成片")
    parser.add_argument("--rubric-iters", type=int, default=0)
    parser.add_argument("--vl", action="store_true", help="VL 视觉评判")
    parser.add_argument("--vl-loop", type=int, default=0)
    parser.add_argument("--director", action="store_true", help="Director 剪辑 Agent")
    parser.add_argument("--ablation", action="store_true", help="2x2 消融（Director × VL）")
    parser.add_argument(
        "--groups", nargs="*", default=None,
        help="只跑指定消融组：baseline / director-only / vl-only / full",
    )
    parser.add_argument("--out", default="eval/report.md")
    args = parser.parse_args()

    settings = Settings.from_env()
    provider = make_provider(settings, "siliconflow")
    cases = select_cases(args.ids)
    try:
        if args.ablation:
            # 2×2 消融：Director（开/关）× VL 评判重选（开/关），共 4 组。
            # 每组都渲染（否则无成片）；VL 相关性仅在有 VL 评判的组有值。
            from eval.report import render_ablation_summary

            groups = [
                ("baseline", dict(rubric_iters=0, run_vl_judge=False, vl_loop=0, director=False)),
                ("director-only", dict(rubric_iters=1, run_vl_judge=False, vl_loop=0, director=True)),
                ("vl-only", dict(rubric_iters=0, run_vl_judge=True, vl_loop=1, director=False)),
                ("full", dict(rubric_iters=1, run_vl_judge=True, vl_loop=1, director=True)),
            ]
            if args.groups:
                groups = [(n, c) for n, c in groups if n in args.groups]
            out_dir = Path(args.out).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            all_payloads: dict[str, dict] = {}
            all_costs: dict[str, dict] = {}
            for name, cfg in groups:
                print(f"\n===== 消融组 {name} =====")
                results, cost = _run_group(
                    settings, provider, cases, label=name,
                    render=args.render, **cfg,
                )
                all_payloads[name] = {"results": results, "cost": cost}
                all_costs[name] = cost
                (out_dir / f"ablation_{name}.md").write_text(
                    render_report(results, title=f"消融 {name}"), encoding="utf-8"
                )
            (out_dir / "ablation_cost.json").write_text(
                json.dumps(all_costs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (out_dir / "ablation_summary.md").write_text(
                render_ablation_summary(all_payloads), encoding="utf-8"
            )
            print(f"\n消融完成，汇总：{out_dir}/ablation_summary.md")
        else:
            results, cost = _run_group(
                settings, provider, cases, label="eval",
                render=args.render, rubric_iters=args.rubric_iters,
                run_vl_judge=args.vl, vl_loop=args.vl_loop, director=args.director,
            )
            report = render_report(results, title="TraceCut 评测报告")
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report, encoding="utf-8")
            (out.parent / "eval_cost.json").write_text(
                json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"报告：{out}；成本：{out.parent}/eval_cost.json")
    finally:
        provider.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
