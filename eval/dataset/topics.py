"""评测测试集：5 个 AI 主题 × 4 个变体 = 20 题。

每题含：
- ``topic``：主题 / 切入角度
- ``source_urls``：可核验的 arXiv 证据（均已通过页面核验，2026-11）
- ``duration_s``：目标时长
- ``difficulty``：``concrete``（B-roll 好匹配）/ ``abstract``（难匹配）
- ``expected_claims``：人工标注的关键主张，供语义评测（LLM-as-judge）对照
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    id: str
    topic: str
    source_urls: list[str]
    duration_s: int
    difficulty: str  # "concrete" | "abstract"
    expected_claims: list[str]


@dataclass(frozen=True)
class Theme:
    key: str
    difficulty: str
    source_urls: list[str]
    expected_claims: list[str]
    variants: list[tuple[str, int]]  # (topic, duration_s)


_THEMES: list[Theme] = [
    Theme(
        key="inference-opt",
        difficulty="abstract",
        source_urls=["https://arxiv.org/abs/2309.06180"],
        expected_claims=[
            "PagedAttention 将 KV 缓存分页管理，避免显存碎片化",
            "vLLM 显著提升了 LLM 推理的吞吐量",
        ],
        variants=[
            ("大模型推理优化", 30),
            ("大语言模型服务的内存管理", 45),
            ("PagedAttention 分页注意力机制", 30),
            ("大模型推理吞吐量优化", 60),
        ],
    ),
    Theme(
        key="multimodal-retrieval",
        difficulty="concrete",
        source_urls=["https://arxiv.org/abs/2103.00020"],
        expected_claims=[
            "CLIP 通过图文对比学习实现零样本图像分类",
            "CLIP 用自然语言监督训练可迁移的视觉模型",
        ],
        variants=[
            ("多模态检索", 30),
            ("CLIP 图文对比学习", 30),
            ("从自然语言监督学习视觉模型", 45),
            ("图文检索与零样本分类", 30),
        ],
    ),
    Theme(
        key="ai-alignment",
        difficulty="abstract",
        source_urls=["https://arxiv.org/abs/2203.02155"],
        expected_claims=[
            "InstructGPT 用人类反馈训练模型遵循指令",
            "RLHF 提升模型与人类意图的对齐程度",
        ],
        variants=[
            ("AI 安全对齐", 30),
            ("大模型指令微调与人类反馈", 45),
            ("RLHF 强化学习人类反馈", 30),
            ("让模型遵循人类指令", 30),
        ],
    ),
    Theme(
        key="distillation",
        difficulty="abstract",
        source_urls=["https://arxiv.org/abs/1503.02531"],
        expected_claims=[
            "知识蒸馏用教师模型的输出训练更小的学生模型",
            "蒸馏让学生模型继承教师模型的知识",
        ],
        variants=[
            ("模型蒸馏", 30),
            ("知识蒸馏", 30),
            ("神经网络知识迁移", 45),
            ("大模型压缩与蒸馏", 60),
        ],
    ),
    Theme(
        key="agent-tooling",
        difficulty="concrete",
        source_urls=["https://arxiv.org/abs/2210.03629"],
        expected_claims=[
            "ReAct 让模型交替进行推理与行动",
            "ReAct 结合推理和工具调用提升任务表现",
        ],
        variants=[
            ("Agent 工具调用", 30),
            ("ReAct 推理与行动", 30),
            ("大模型推理与行动协同", 45),
            ("语言模型使用工具", 30),
        ],
    ),
]


def build_cases() -> list[EvalCase]:
    """由主题定义展开为 20 题测试集。"""
    cases: list[EvalCase] = []
    for theme in _THEMES:
        for i, (topic, dur) in enumerate(theme.variants, start=1):
            cases.append(
                EvalCase(
                    id=f"{theme.key}-v{i}",
                    topic=topic,
                    source_urls=list(theme.source_urls),
                    duration_s=dur,
                    difficulty=theme.difficulty,
                    expected_claims=list(theme.expected_claims),
                )
            )
    return cases


CASES: list[EvalCase] = build_cases()
