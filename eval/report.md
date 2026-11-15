# TraceCut 评测报告

- 题目数：**8**
- 平均 claim_provenance_coverage：**1.0**
- 平均 scene_evidence_coverage：**1.0**
- 平均 media_coverage：**1.0**
- 平均忠实度（1-5）：**4.75**
- 平均 VL 相关性：**2.304**
- 平均误导比例：**0.658**
- 平均端到端耗时：**938.425s**

## 按难度分组

### concrete（3 题）
- 忠实度：4.667
- VL 相关性：2.667
- 误导比例：0.717
- media_coverage：1.0

### abstract（5 题）
- 忠实度：4.8
- VL 相关性：2.086
- 误导比例：0.623
- media_coverage：1.0

## 明细

| case | 主题 | 难度 | claim覆盖 | 媒体覆盖 | 忠实度 | VL相关性 | 误导比 | 耗时s |
|---|---|---|---|---|---|---|---|---|
| inference-opt-v1 | 大模型推理优化 | abstract | 1.0 | 1.0 | 5 | 1.8 | 0.8 | 867.9 |
| inference-opt-v2 | 大语言模型服务的内存管理 | abstract | 1.0 | 1.0 | 5 | 2.429 | 0.7142857142857143 | 1202.3 |
| ai-alignment-v1 | AI 安全对齐 | abstract | 1.0 | 1.0 | 4 | 2.0 | 0.4 | 1132.6 |
| ai-alignment-v2 | 大模型指令微调与人类反馈 | abstract | 1.0 | 1.0 | 5 | 2.0 | 0.2 | 1136.4 |
| distillation-v1 | 模型蒸馏 | abstract | 1.0 | 1.0 | 5 | 2.2 | 1.0 | 1038.5 |
| agent-tooling-v1 | Agent 工具调用 | concrete | 1.0 | 1.0 | 4 | 2.6 | 0.6 | 1078.5 |
| multimodal-retrieval-v1 | 多模态检索 | concrete | 1.0 | 1.0 | 5 | 2.4 | 0.8 | 659.3 |
| multimodal-retrieval-v2 | CLIP 图文对比学习 | concrete | 1.0 | 1.0 | 5 | 3.0 | 0.75 | 391.9 |
