"""Gradio UI for the TraceCut demo.

Run with:  python -m serve.ui

渲染约定（排版约束）：
- 右列从上到下：状态面板（固定卡片）→ 成片视频 → 溯源表格 → 指标/成本两列。
- 生成过程中只有一个"状态面板"实时更新（过程词轮换 + 进度条 + 已用时间），
  其余输出组件用 ``gr.skip()`` 保持原值 —— 避免 Gradio 对每个输出组件都叠加
  进度覆盖层造成的"多框重复"，也避免空组件高度塌陷导致的元素重叠。
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

import gradio as gr

from serve.provenance import render_provenance_html
from serve.worker import JobManager
from tracecut.config import Settings

settings = Settings.from_env()
manager = JobManager(settings)

# 阶段性过程词：同一阶段内按时间轮换，制造"正在干活"的动态反馈。
PHRASES: dict[str, list[str]] = {
    "start": ["正在唤醒流水线…"],
    "evidence": ["正在检索证据来源…", "快速阅读文献中…", "抓取页面正文…"],
    "plan": ["快速思考中…", "正在拆解事实主张…", "构建证据图谱…", "撰写旁白文案…"],
    "media": ["正在挑选 B-roll 素材…", "匹配画面与旁白…", "下载视频素材…"],
    "evaluate": ["正在审校事实…", "交叉核对引用…"],
    "rubric": ["自我迭代优化中…", "润色文案与节奏…"],
    "director": ["剪辑 Agent 决策中…", "调整转场与花字…"],
    "render": ["正在渲染画面…", "合成语音与字幕…", "拼接分镜…"],
    "vl": ["视觉质检中…", "核对画面与旁白…"],
    "done": ["收尾整理…"],
}
DEFAULT_PHRASES = PHRASES["plan"]

PHRASE_ROTATE_S = 2.0  # 过程词轮换间隔
POLL_INTERVAL_S = 0.6  # 任务状态轮询间隔

_STATUS_CSS = """
.tc-status{padding:14px 18px;border:1px solid #2a3550;border-radius:10px;
  background:#0d1424;font-family:inherit}
.tc-row{display:flex;align-items:center;gap:10px}
.tc-dot{width:9px;height:9px;border-radius:50%;background:#ff7a1a;flex:none;
  animation:tc-blink 1s ease-in-out infinite}
.tc-dot.tc-ok{background:#31c48d;animation:none}
.tc-dot.tc-cancel{background:#e5484d;animation:none}
.tc-phrase{color:#e8ecf5;font-size:15px;font-weight:600;letter-spacing:.3px}
.tc-bar{height:8px;background:#1c2438;border-radius:4px;margin:12px 0 8px;
  overflow:hidden}
.tc-fill{height:100%;border-radius:4px;
  background:linear-gradient(90deg,#ff7a1a,#ffb066);transition:width .5s ease}
.tc-meta{color:#8b93a7;font-size:12px}
@keyframes tc-blink{0%,100%{opacity:1}50%{opacity:.25}}
"""


def _esc(text: str) -> str:
    return html.escape(text or "")


def _json_html(obj: Any) -> str:
    """把 dict 渲染为带样式的 <pre> JSON 块（替代 gr.JSON，规避 gradio 4.44 的 schema bug）。"""
    if obj is None:
        obj = {}
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    return (
        "<pre style='max-height:320px;overflow:auto;background:#0d1424;color:#e8ecf5;"
        f"padding:12px;border-radius:8px;font-size:12px;line-height:1.5'>{_esc(text)}</pre>"
    )


def _status_html(
    phrase: str, stage: str, percent: float, elapsed: float,
    *, ok: bool = False, cancelled: bool = False,
) -> str:
    fill = max(0.0, min(100.0, percent))
    if cancelled:
        dot_cls = "tc-dot tc-cancel"
    elif ok:
        dot_cls = "tc-dot tc-ok"
    else:
        dot_cls = "tc-dot"
    return (
        f'<div class="tc-status">'
        f'<div class="tc-row"><span class="{dot_cls}"></span>'
        f'<span class="tc-phrase">{_esc(phrase)}</span></div>'
        f'<div class="tc-bar"><div class="tc-fill" style="width:{fill:.0f}%"></div></div>'
        f'<div class="tc-meta">阶段 {_esc(stage)} · {percent:.0f}% · 已用 {elapsed:.0f}s</div>'
        f"</div>"
        f"<style>{_STATUS_CSS}</style>"
    )


def _load_manifest(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


# 当前正在生成的任务 id，供"取消生成"按钮回调读取。
_current_job_id: str | None = None


def cancel_current() -> str:
    """请求取消当前正在生成的任务（阶段边界生效）。"""
    global _current_job_id
    if not _current_job_id:
        return "当前没有进行中的任务"
    if manager.cancel(_current_job_id):
        return "已请求取消，将在当前阶段结束后终止"
    return "取消失败：任务不存在"


def generate(
    topic: str,
    source_urls: str,
    duration_s: int,
    render: bool,
    rubric_iters: int,
    run_vl_judge: bool,
    vl_loop: int,
    director: bool,
):
    if not topic or not topic.strip():
        raise gr.Error("请输入 topic")
    global _current_job_id
    urls = [u.strip() for u in source_urls.split("\n") if u.strip()]
    job_id = manager.submit(
        topic.strip(),
        urls,
        duration_s=int(duration_s),
        render=bool(render),
        rubric_iters=int(rubric_iters),
        run_vl_judge=bool(run_vl_judge),
        vl_loop=int(vl_loop),
        director=bool(director),
    )
    _current_job_id = job_id

    # 生成中：只更新状态面板（过程词按定时器轮换），其余组件 gr.skip() 保持原值。
    t0 = time.time()
    skip = gr.skip()
    yield [skip, skip, skip, skip, _status_html("正在提交任务…", "start", 0.0, 0.0)]
    try:
        while True:
            job = manager.get(job_id)
            if job is None:
                raise gr.Error("任务丢失（job not found）")
            stage = job["stage"] or "start"
            pct = job["percent"]
            elapsed = time.time() - t0
            phrases = PHRASES.get(stage, DEFAULT_PHRASES)
            phrase = phrases[int(elapsed / PHRASE_ROTATE_S) % len(phrases)]
            yield [skip, skip, skip, skip, _status_html(phrase, stage, pct, elapsed)]
            if job["status"] == "cancelled":
                yield [skip, skip, skip, skip,
                       _status_html("已取消", "cancelled", pct, elapsed, cancelled=True)]
                return
            if job["status"] == "failed":
                raise gr.Error(job["error"])
            if job["status"] == "done":
                break
            time.sleep(POLL_INTERVAL_S)
    finally:
        _current_job_id = None

    manifest = _load_manifest(job["manifest_path"]) or {}
    video = manifest.get("output_video", "")
    provenance_html = render_provenance_html(manifest)
    evaluation = manifest.get("evaluation", {})
    cost = job["cost"]
    warnings_text = "\n".join(f"- {w}" for w in manifest.get("warnings", [])) or "（无）"
    final_panel = _status_html(
        "生成完成 ✓", "done", 100.0, job["elapsed_s"], ok=True
    ) + f"<div class='tc-meta' style='margin-top:8px'>Warnings:<br>{_esc(warnings_text)}</div>"
    yield [video, provenance_html, _json_html(evaluation), _json_html(cost), final_panel]


with gr.Blocks(title="TraceCut Demo", analytics_enabled=False) as demo:
    gr.Markdown(
        "# TraceCut · 证据可追溯的自动短视频生成\n"
        "输入主题与证据源 URL，一键生成带溯源图的竖屏短视频。"
    )
    with gr.Row():
        with gr.Column(scale=1):
            topic = gr.Textbox(label="主题（topic）", placeholder="黑洞照片是怎么拍出来的")
            source_urls = gr.Textbox(
                label="证据源 URL（每行一个，可选）",
                placeholder="https://arxiv.org/abs/1906.11238",
                lines=3,
            )
            duration = gr.Slider(
                15, 90, value=30, step=5, label="目标时长（秒）",
                info="期望成片时长，为软指引：实际时长由旁白内容决定",
            )
            render = gr.Checkbox(
                value=True, label="生成视频文件",
                info="勾选后渲染出 MP4 成片；不勾选仅生成计划与素材清单",
            )
            rubric_iters = gr.Slider(
                0, 3, value=1, step=1, label="内容自优化轮数",
                info="Rubric 六维评分 + 软编辑迭代次数，0=关闭；越大文案打磨越充分",
            )
            run_vl = gr.Checkbox(
                value=True, label="画面质量自检",
                info="用 Qwen3-VL 逐镜头评估画面-旁白相关性（即 VL 视觉评判）",
            )
            vl_loop = gr.Slider(
                0, 2, value=1, step=1, label="画面素材优化轮数",
                info="对低相关镜头自动重选 B-roll 素材的轮数，隐含开启画面自检",
            )
            director = gr.Checkbox(
                value=False, label="剪辑 Agent",
                info="多轮工具调用的剪辑决策 Agent，需内容自优化轮数 ≥1 才生效",
            )
            with gr.Row():
                btn = gr.Button("生成", variant="primary")
                cancel_btn = gr.Button("取消生成", variant="stop")
            cancel_feedback = gr.Markdown("")
        with gr.Column(scale=2):
            status_panel = gr.HTML(
                value=_status_html("等待输入主题…", "idle", 0.0, 0.0),
                label="生成状态",
            )
            video = gr.Video(label="成片")
            provenance = gr.HTML(label="溯源图（claim → 证据 → 分镜 → 素材）")
            with gr.Row():
                evaluation = gr.HTML(label="结构指标")
                cost = gr.HTML(label="成本报告")

    btn.click(
        generate,
        inputs=[topic, source_urls, duration, render, rubric_iters, run_vl, vl_loop, director],
        outputs=[video, provenance, evaluation, cost, status_panel],
    )
    cancel_btn.click(cancel_current, outputs=[cancel_feedback])


if __name__ == "__main__":
    demo.queue().launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
    )
