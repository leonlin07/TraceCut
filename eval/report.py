"""评测报告生成：把聚合指标渲染为 Markdown 报告。"""

from __future__ import annotations

from typing import Any


def _avg(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def render_report(
    results: list[dict[str, Any]], title: str = "TraceCut 评测报告"
) -> str:
    """把 ``aggregate_metrics`` 的结果列表渲染为 Markdown 报告。"""
    lines = [f"# {title}", ""]

    def _faith(r: dict[str, Any]) -> int | None:
        f = r.get("faithfulness") or {}
        return f.get("score")

    n = len(results)
    claim_cov = _avg([r["claim_provenance_coverage"] for r in results])
    scene_cov = _avg([r["scene_evidence_coverage"] for r in results])
    media_cov = _avg([r["media_coverage"] for r in results])
    faith = _avg([_faith(r) for r in results])
    vl = _avg([r["vl_relevance_avg"] for r in results])
    mislead = _avg([r["misleading_ratio"] for r in results])
    elapsed = _avg([r.get("elapsed_s") for r in results])

    lines += [
        f"- 题目数：**{n}**",
        f"- 平均 claim_provenance_coverage：**{claim_cov}**",
        f"- 平均 scene_evidence_coverage：**{scene_cov}**",
        f"- 平均 media_coverage：**{media_cov}**",
        f"- 平均忠实度（1-5）：**{faith}**",
        f"- 平均 VL 相关性：**{vl}**",
        f"- 平均误导比例：**{mislead}**",
        f"- 平均端到端耗时：**{elapsed}s**",
        "",
    ]

    lines += ["## 按难度分组", ""]
    for diff in ("concrete", "abstract"):
        subset = [r for r in results if r["difficulty"] == diff]
        if not subset:
            continue
        lines += [
            f"### {diff}（{len(subset)} 题）",
            f"- 忠实度：{_avg([_faith(r) for r in subset])}",
            f"- VL 相关性：{_avg([r['vl_relevance_avg'] for r in subset])}",
            f"- 误导比例：{_avg([r['misleading_ratio'] for r in subset])}",
            f"- media_coverage：{_avg([r['media_coverage'] for r in subset])}",
            "",
        ]

    lines += [
        "## 明细",
        "",
        "| case | 主题 | 难度 | claim覆盖 | 媒体覆盖 | 忠实度 | VL相关性 | 误导比 | 耗时s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        f = r.get("faithfulness") or {}
        lines.append(
            f"| {r['case_id']} | {r['topic']} | {r['difficulty']} "
            f"| {r['claim_provenance_coverage']} | {r['media_coverage']} "
            f"| {f.get('score')} | {r['vl_relevance_avg']} | {r['misleading_ratio']} "
            f"| {r.get('elapsed_s')} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_ablation_summary(groups: dict[str, dict[str, Any]]) -> str:
    """对比多组消融的平均指标，输出汇总 Markdown。

    ``groups`` 形如 ``{name: {"results": [...], "cost": {...}}}``。
    """
    lines = ["# 消融实验汇总", ""]
    lines += [
        "| 配置 | 忠实度 | VL相关性 | 误导比 | media覆盖 | 平均耗时s | 总成本¥ |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, payload in groups.items():
        results = payload["results"]
        cost = payload["cost"]
        faith = _avg([(r.get("faithfulness") or {}).get("score") for r in results])
        vl = _avg([r["vl_relevance_avg"] for r in results])
        mislead = _avg([r["misleading_ratio"] for r in results])
        media = _avg([r["media_coverage"] for r in results])
        elapsed = _avg([r.get("elapsed_s") for r in results])
        total_cost = cost.get("total_cost_rmb")
        lines.append(
            f"| {name} | {faith} | {vl} | {mislead} | {media} | {elapsed} | {total_cost} |"
        )
    lines.append("")
    lines.append("> 注：VL 相关性仅在有 VL 评判的组（vl-only / full）有值；忠实度/覆盖率为全组可对比。")
    lines.append("")
    return "\n".join(lines)
