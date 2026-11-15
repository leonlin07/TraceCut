<div align="center">

# TraceCut

**证据约束 · 全链路可追溯** 的自动短视频生成

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)]()
[![FFmpeg](https://img.shields.io/badge/FFmpeg-required-007808)]()
[![Status](https://img.shields.io/badge/status-prototype-orange)]()
[![License](https://img.shields.io/badge/license-research--only-lightgrey)]()

</div>

---

**TraceCut** 面向开放网页与可授权素材，把自动成片从"生成一个像视频的东西"升级为"生成一个**每个事实、镜头、素材都可追溯**的产物"。给定方向与公开来源，系统先建立 `claim → evidence → scene → material` 图，再生成旁白、检索 B-roll、合成竖屏短视频并自动审计——研究重点是**事实忠实度、视觉相关性、来源可追溯性与制作预算能否同时成立**。

## 快速了解

- **完整说明与使用文档**：见 [`tracecut/README.md`](tracecut/README.md)
- **项目路线与研究问题**：见 [`docs/ROADMAP.md`](docs/ROADMAP.md)

## 目录结构

| 目录 | 说明 |
|---|---|
| `tracecut/` | 核心 Python 包（证据检索 / 规划 / 渲染 / 评测 / 谣言判别） |
| `serve/` | Gradio 交互界面 + FastAPI 服务 |
| `cost/` | 成本统计模块 |
| `eval/` | 评测脚本与消融报告 |
| `experiments/` | 谣言判别实验 |
| `docs/` | 项目文档与 Demo 素材 |

## 快速开始

```bash
cd tracecut
pip install -e '.[dev]'

export SILICONFLOW_API_KEY="sk-xxx"
export PEXELS_API_KEY="xxx"

tracecut doctor

tracecut run "Transformer 是如何提出的" \
  --source-url "https://arxiv.org/abs/1706.03762" \
  --render
```

## 许可

研究原型，仅供学术研究使用。外部依赖（SiliconFlow / Pexels / Brave / Wikipedia 等）各自受其服务条款约束。
