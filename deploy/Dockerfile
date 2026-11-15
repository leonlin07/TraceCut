# TraceCut 应用型 Demo —— 证据可追溯的自动短视频生成
FROM python:3.11-slim

# 系统依赖：
#   - ffmpeg：渲染 / BGM 混音 / 字幕烧录
#   - fonts-noto-cjk：中文字体（media.py 的 _CJK_FONT_CANDIDATES 会自动探测
#     /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 源码：核心包 + 服务层 + 成本模块
COPY tracecut/pyproject.toml tracecut/README.md /app/tracecut/
COPY tracecut/src /app/tracecut/src
COPY serve /app/serve
COPY cost /app/cost

# 安装核心依赖（httpx/openai/pydantic）+ 服务层依赖（fastapi/uvicorn/gradio）
RUN pip install --no-cache-dir -e /app/tracecut \
    && pip install --no-cache-dir -r /app/serve/requirements.txt

# Gradio 监听 0.0.0.0（容器外可访问）；成片输出到 /app/runs（volume 挂载）
ENV GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860 \
    TRACECUT_OUTPUT_DIR=/app/runs

EXPOSE 7860 8000

# 默认启动 Gradio UI。
# FastAPI 备选：uvicorn serve.app:app --host 0.0.0.0 --port 8000
CMD ["python", "-m", "serve.ui"]
