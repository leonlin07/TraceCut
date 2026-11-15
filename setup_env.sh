#!/usr/bin/env bash
# 一键安装 TraceCut 运行环境（Linux / macOS）
# 环境名：video_edit
#
# 运行方式：
#   chmod +x setup_env.sh
#   ./setup_env.sh

set -e

ENV_NAME="video_edit"
PYTHON_VER="3.11"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
TRACECUT_DIR="$PROJECT_ROOT/tracecut"

if ! command -v conda &> /dev/null; then
    echo "错误：未检测到 conda。请先安装 Miniconda 或 Anaconda。"
    echo "建议下载地址: https://docs.conda.io/en/latest/miniconda.html"
    exit 1
fi

echo "=============================="
echo "TraceCut 环境一键安装脚本"
echo "环境名: $ENV_NAME | Python: $PYTHON_VER"
echo "=============================="

echo ""
echo "[1/4] 检查并创建 conda 环境 '$ENV_NAME'..."
if ! conda env list | grep -q "^$ENV_NAME "; then
    conda create -n "$ENV_NAME" python="$PYTHON_VER" -y
else
    echo "环境已存在，跳过创建。"
fi

echo ""
echo "[2/4] 安装 ffmpeg..."
conda install -n "$ENV_NAME" ffmpeg -c conda-forge -y

echo ""
echo "[3/4] 安装 tracecut (editable)..."
conda run -n "$ENV_NAME" --live-stream pip install -e "$TRACECUT_DIR"

echo ""
echo "[4/4] 安装开发依赖 (pytest, ruff)..."
conda run -n "$ENV_NAME" --live-stream pip install -e "$TRACECUT_DIR[dev]"

echo ""
echo "=============================="
echo "安装完成！"
echo "激活环境: conda activate $ENV_NAME"
echo "运行测试: python -m pytest tracecut/tests/ -v"
echo "批量生成: python branch_7/batch.py --topics topics.txt --render"
echo "=============================="
