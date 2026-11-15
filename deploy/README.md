# TraceCut Demo 部署指南（M3）

把 TraceCut 应用型 Demo 打包成 Docker 镜像，本地一键起服务，或部署到任意云主机生成可分享链接。

## 1. 前置要求

- Docker（含 Docker Compose 插件）
- 两个 API key：
  - `SILICONFLOW_API_KEY`（硅基流动，Planner / Reviewer / TTS）
  - `PEXELS_API_KEY`（Pexels 素材库，B-roll 检索）

## 2. 快速开始（本地）

```powershell
# 在项目根目录下
cd <project-root>

# 方式一：显式传环境变量
$env:SILICONFLOW_API_KEY="sk-xxxx"
$env:PEXELS_API_KEY="xxxx"
docker compose -f deploy/docker-compose.yml up --build -d

# 方式二：写进项目根 .env（docker compose 会自动读取）
# .env 内容见下
```

浏览器打开 http://localhost:7860 （Gradio UI）。

FastAPI 接口：http://localhost:8000/docs

## 3. 环境变量（.env 示例）

```env
SILICONFLOW_API_KEY=sk-xxxx
PEXELS_API_KEY=xxxx
# 可选覆盖（默认值已写在 docker-compose.yml）
# SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
# SILICONFLOW_MODEL=Qwen/Qwen3.5-35B-A3B
```

## 4. 常用命令

```powershell
# 构建并后台启动
docker compose -f deploy/docker-compose.yml up --build -d

# 查看日志
docker compose -f deploy/docker-compose.yml logs -f

# 停止
docker compose -f deploy/docker-compose.yml down

# 停止并清空成片卷
docker compose -f deploy/docker-compose.yml down -v
```

## 5. 部署到云主机（可分享链接）

1. 在云主机安装 Docker；
2. 上传项目（或 `git clone`），把 `.env` 放到项目根；
3. 执行 `docker compose -f deploy/docker-compose.yml up --build -d`；
4. 云主机安全组放行 7860 端口；
5. 分享 `http://<公网IP>:7860`。

> 生产建议：前置 Nginx + HTTPS（Gradio 需要 HTTPS 才能用麦克风等浏览器能力，纯文本输入不受影响），并给 FastAPI 加鉴权。当前为 demo 形态，未加鉴权，请勿在公网无保护暴露。

## 6. 镜像内容说明

| 组件 | 说明 |
|---|---|
| 基础镜像 | `python:3.11-slim` |
| 系统依赖 | `ffmpeg`（渲染/混音）、`fonts-noto-cjk`（中文字幕/标题卡） |
| Python 依赖 | `httpx openai pydantic`（核心）+ `fastapi uvicorn gradio`（服务层） |
| 启动方式 | 默认 `python -m serve.ui`（Gradio，监听 0.0.0.0:7860） |
| 备选启动 | `uvicorn serve.app:app --host 0.0.0.0 --port 8000`（FastAPI） |
| 成片持久化 | volume `tracecut_runs` 挂载 `/app/runs` |

## 7. 已知限制

- **BGM/SFX**：镜像不含本地 BGM/SFX 资产（`assets/` 未打包），运行时走**程序化和弦垫乐**（`bgm.py::gen_procedural_bgm`，零外部依赖）。如需真实曲库，挂载 `TRACECUT_BGM_DIR` 到含 mp3/wav 的目录。
- **模型网关**：容器默认走 SiliconFlow（公网）。
- **VL 相关性偏低**（8 题均值 2.3/5）：瓶颈在 Pexels 通用素材对科普概念的匹配度，属已知研究问题，非部署缺陷。
- **无鉴权**：demo 形态未加登录，公网部署务必加 Nginx/HTTPS/鉴权。
