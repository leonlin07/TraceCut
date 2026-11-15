@echo off
chcp 65001 >nul
REM 一键安装 TraceCut 运行环境（Windows CMD）
REM 环境名：video_edit
REM
REM 运行方式：双击或在 CMD 中执行 setup_env.bat

setlocal
set "envName=video_edit"
set "pythonVer=3.11"

REM 定位项目根目录
set "projectRoot=%~dp0"
set "tracecutDir=%projectRoot%tracecut"

where conda >nul 2>nul
if errorlevel 1 (
    echo 错误：未检测到 conda。请先安装 Miniconda 或 Anaconda。
    echo 建议下载地址: https://docs.conda.io/en/latest/miniconda.html
    exit /b 1
)

echo ==============================
echo TraceCut 环境一键安装脚本
echo 环境名: %envName% ^| Python: %pythonVer%
echo ==============================

echo.
echo [1/4] 检查并创建 conda 环境 '%envName%'...
conda env list | findstr /B "%envName% " >nul
if errorlevel 1 (
    conda create -n %envName% python=%pythonVer% -y
) else (
    echo 环境已存在，跳过创建。
)

echo.
echo [2/4] 安装 ffmpeg...
conda install -n %envName% ffmpeg -c conda-forge -y

echo.
echo [3/4] 安装 tracecut (editable)...
conda run -n %envName% --live-stream pip install -e "%tracecutDir%"

echo.
echo [4/4] 安装开发依赖 (pytest, ruff)...
conda run -n %envName% --live-stream pip install -e "%tracecutDir%[dev]"

echo.
echo ==============================
echo 安装完成！
echo 激活环境: conda activate %envName%
echo 运行测试: python -m pytest tracecut/tests/ -v
echo 批量生成: python branch_7/batch.py --topics topics.txt --render
echo ==============================
endlocal
