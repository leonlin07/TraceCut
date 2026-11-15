# TraceCut Demo 主题清单 + 完整验证计划（M2 / M3）

> 用途：① 为 M2 评测测试集提供 5 类主题与可核验证据源；② 定义 M2/M3 的验证目标、验收标准、指标基线、推进条件。
> 证据 URL 均已通过 arXiv 页面核验（标题/作者/日期匹配）。
> 更新日期：2026-11-10

---

# 第一部分：5 个 AI 主题 + 可核验证据 URL

> 全部选用 arXiv 论文（本项目网络环境下 arXiv 可直连、正文可被 EvidenceCollector 抽取，且标题/日期已核验）。

| # | 主题 | 证据 URL | 标题 | 来源类型 | 发布时间 |
|---|------|---------|------|---------|---------|
| 1 | 大模型推理优化 | https://arxiv.org/abs/2309.06180 | Efficient Memory Management for LLM Serving with PagedAttention（vLLM） | 论文 + 开源仓库（vllm-project/vllm） | 2023-09-12 |
| 2 | 多模态检索 | https://arxiv.org/abs/2103.00020 | Learning Transferable Visual Models From Natural Language Supervision（CLIP） | 论文（OpenAI） | 2021-02-26 |
| 3 | AI 安全对齐 | https://arxiv.org/abs/2203.02155 | Training language models to follow instructions with human feedback（InstructGPT） | 论文（OpenAI） | 2022-03-04 |
| 4 | 模型蒸馏 | https://arxiv.org/abs/1503.02531 | Distilling the Knowledge in a Neural Network（Hinton 等） | 论文（NIPS 2014 workshop） | 2015-03-09 |
| 5 | Agent 工具调用 | https://arxiv.org/abs/2210.03629 | ReAct: Synergizing Reasoning and Acting in Language Models | 论文 + 开源实现（LangChain/llama-index） | 2022-10-06 |

**使用方式**：以上 URL 直接作为 `tracecut run --source-url <url>` 的种子证据，或作为 M2 测试集每题的 `source-url` 字段。

**主题选择理由**：这 5 个主题正好覆盖本项目 pipeline 最看重的三类能力——（a）抽象技术概念能否被准确证据化（推理优化/蒸馏）；（b）视觉/多模态匹配（多模态检索）；（c）事实安全边界（对齐、工具调用）。同时它们都是面试高频方向，生成的成片本身就是谈资。

---

# 第二部分：完整验证计划（M2 / M3）

## 1. 验证目标与验收标准

### M2 —— 评测 + 成本（可量化）

**目标**：建立语义评测体系 + 成本观测，产出可引用的消融报告。

**验收标准**（全部满足才算 M2 完成）：
1. 20 题测试集跑通，每题产出 `manifest.json`（含 coverage / rubric / vl）。
2. 语义评测脚本可输出三个维度分数：**忠实度 / 相关性 / 误导性**，且对同一批题可复现。
3. 消融对比报告：`有无 Director`、`有无 VL 重排` 两组的指标有**可量化的数字差异**（不是"都差不多"）。
4. 成本报告：单次生成费用（¥）、token 分布、按模型分解，误差在价格表精度内。
5. 结构性指标基线（M0 记录）与优化后指标对比，有明确提升或可解释的持平。

### M3 —— 产品化（可分享）

**目标**：把 Demo 变成"发给别人一条链接就能用"。

**验收标准**：
1. Docker 镜像可 `docker compose up` 一键启动。
2. 部署到云主机后公网可访问，生成链路在部署环境跑通至少 1 次。
3. 30–60s 演示录屏 + 架构图 + README。
4. 部署环境有 .env 隔离（密钥不进镜像）。

---

## 2. 测试场景与数据集

### 测试集（20 题）

| 维度 | 设计 |
|------|------|
| 主题分布 | 用第一部分 5 个主题 × 每题 4 个变体（不同切入角度/时长） |
| 每题字段 | `topic`、`source_urls[]`、`duration_s`、`预期关键 claim`（人工标注，供语义评测参照） |
| 难度分层 | 10 题"具象"（如多模态检索、工具调用，B-roll 好匹配）+ 10 题"抽象"（如对齐、蒸馏，B-roll 难匹配） |

### 测试场景（三种运行方式都要覆盖）

| 场景 | 验证点 |
|------|--------|
| CLI `tracecut run` | 核心链路正确性、`--director`/`--vl` 标志生效 |
| FastAPI `/generate → /status → /result` | 异步任务、进度回调、结果聚合 |
| Gradio UI | 端到端交互、进度刷新、溯源图/成本展示 |

---

## 3. 指标与基线

### 结构性指标（已有，确定性）

| 指标 | 含义 | 基线来源 |
|------|------|---------|
| `claim_provenance_coverage` | claim 有证据支撑的比例 | M0 baseline |
| `scene_evidence_coverage` | 分镜引用有效证据的比例 | M0 baseline |
| `media_coverage` | 分镜有 B-roll 素材的比例 | M0 baseline（此前修复后已达 1.0） |

### 语义指标（新增，LLM-as-judge）

| 指标 | 定义 | 打分方式 |
|------|------|---------|
| 忠实度 | 旁白是否忠于证据、有无无源推断 | LLM 对照证据摘要打分 1–5 |
| 相关性 | 画面/旁白与主题的贴合度 | 复用 VL judge（已有） |
| 误导性 | 素材是否易被误认为真实证据 | 复用 `misleading_visual`（已有） |

### 基线（M0 必须先行冻结）

- 跑 3 条"无 Director、无 VL 重排"的成片作为 baseline，记录 coverage / vl 均分 / 单次成本 / 耗时。
- 后续所有优化都以这组基线做增量对比。

---

## 4. 环境与工具

| 项 | 说明 |
|----|------|
| 环境 | conda `video_edit`（已含 ffmpeg、tracecut、fastapi/uvicorn/gradio） |
| 密钥 | `SILICONFLOW_API_KEY`、`PEXELS_API_KEY`（.env，已被 gitignore） |
| 网络 | arXiv 可直连；Wikipedia 被墙，测试集 source-url 一律用 arXiv |
| 测试 | `pytest`（离线单测）+ `tracecut doctor`（环境自检） |
| 观测 | `cost/tracker.py`（token/费用）、`serve/worker.py`（耗时） |

---

## 5. 时间线与责任分工

> 单人项目，责任分工按"阶段负责人 = 同一人，但按模块切分验收"来写。

| 阶段 | 周期 | 交付物 |
|------|------|--------|
| M2-a 测试集 + 语义评测脚本 | 第 1 周 | 20 题 dataset + `eval/metrics.py` |
| M2-b 消融实验 | 第 2 周 | 有无 Director / 有无 VL 重排 对比报告 |
| M2-c 成本报告 | 第 2–3 周 | 单次/月度成本 + 模型路由建议 |
| M3-a Docker 化 | 第 3–4 周 | Dockerfile + compose |
| M3-b 部署 + 录屏 | 第 4 周 | 公网链接 + 录屏 + README |

---

## 6. 风险与回滚策略

| 风险 | 影响 | 缓解 / 回滚 |
|------|------|------------|
| arXiv 偶发连接重置（曾见 10054） | 评测中断 | 测试集 URL 缓存 + 重试；证据源固定 arXiv 稳定编号 |
| 免费模型质量下降（Director/Rubric 降级） | 断链率上升、卖点受损 | 保留"核心用付费 35B-A3B"的混合路由；可一键切回付费 |
| Director 多轮工具调用走样 | 溯源图被破坏 | pipeline 已有不变量校验；Director=off 的基线始终保留 |
| 成本超支 | 预算失控 | 月度上限告警 + 免费模型路由；全免费仅演示用 |
| 部署环境密钥泄露 | 安全 | .env 不进镜像、不入库（已 gitignore） |
| 指标"看起来差不多"（消融无差异） | 报告说服力弱 | 提前用"难度分层"测试集放大差异；必要时加人工抽查样本 |

**回滚总原则**：每个里程碑用 git tag 打点，任何一步失败都能回退到上一个可工作版本；核心 pipeline 改动都有独立离线单测守护。

---

## 7. M2 / M3 推进条件（进入 / 退出准则）

### M2（评测 + 成本）

| 项 | 内容 |
|----|------|
| **进入准则** | ① M1 服务化完成且本地可交互（✅ 已达成）；② 20 题测试集就绪；③ M0 baseline 已冻结 |
| **依赖项** | 测试集（含 arXiv URL）、`eval/metrics.py`（未建）、`cost/tracker.py`（✅ 已建） |
| **退出准则** | ① 消融报告产出且指标有可量化差异；② 成本报告产出；③ 语义评测脚本可复现 |
| **建议启动时间** | **立即**（M1 已就绪，唯一前置是补 20 题测试集，第 1 周即可产出） |

### M3（产品化）

| 项 | 内容 |
|----|------|
| **进入准则** | ① M2 退出（消融/成本报告无致命问题）；② 至少 3 条可展示成片 |
| **依赖项** | Docker 环境、云主机或公网发布渠道、录屏工具 |
| **退出准则** | ① Docker 一键启动；② 公网可访问且跑通 1 次；③ 录屏 + README + 架构图 |
| **建议启动时间** | M2 完成后（约第 3–4 周），与 M2-c 成本报告可并行收尾 |

---

## 附：一句话优先级

> **先冻结 M0 baseline → 补 20 题测试集 → 跑消融拿数字 → 再谈部署。** 数字（消融报告）是面试弹药，部署（M3）是加分项，不要在没有数字前先做 Docker。
