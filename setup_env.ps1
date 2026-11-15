# 一键安装 TraceCut 运行环境（PowerShell）
# 环境名：video_edit
#
# 运行方式：
#   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
#   .\setup_env.ps1

$ErrorActionPreference = "Stop"

$envName = "video_edit"
$pythonVer = "3.11"

# 定位项目根目录（脚本所在目录）
$projectRoot = $PSScriptRoot
if (-not $projectRoot) {
    $projectRoot = Get-Location
}
$tracecutDir = Join-Path $projectRoot "tracecut"

function Test-Command($cmd) {
    return [bool](Get-Command $cmd -ErrorAction SilentlyContinue)
}

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "TraceCut 环境一键安装脚本" -ForegroundColor Cyan
Write-Host "环境名: $envName | Python: $pythonVer" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan

# 1. 检查 conda
if (-not (Test-Command conda)) {
    Write-Host "错误：未检测到 conda。请先安装 Miniconda 或 Anaconda。" -ForegroundColor Red
    Write-Host "建议下载地址: https://docs.conda.io/en/latest/miniconda.html"
    exit 1
}

# 2. 创建/更新 conda 环境
Write-Host "`n[1/4] 检查并创建 conda 环境 '$envName'..." -ForegroundColor Green
$envList = conda env list 2>&1
$envExists = $envList | Select-String "^$envName\s+"
if (-not $envExists) {
    conda create -n $envName python=$pythonVer -y
} else {
    Write-Host "环境已存在，跳过创建。" -ForegroundColor Yellow
}

# 3. 安装 ffmpeg（渲染必需）
Write-Host "`n[2/4] 安装 ffmpeg..." -ForegroundColor Green
conda install -n $envName ffmpeg -c conda-forge -y

# 4. 安装 tracecut 包（editable）
Write-Host "`n[3/4] 安装 tracecut (editable)..." -ForegroundColor Green
conda run -n $envName --live-stream pip install -e "$tracecutDir"

# 5. 安装开发依赖
Write-Host "`n[4/4] 安装开发依赖 (pytest, ruff)..." -ForegroundColor Green
conda run -n $envName --live-stream pip install -e "$tracecutDir[dev]"

Write-Host "`n======================================" -ForegroundColor Green
Write-Host "安装完成！" -ForegroundColor Green
Write-Host "激活环境: conda activate $envName" -ForegroundColor Green
Write-Host "运行测试: python -m pytest tracecut/tests/ -v" -ForegroundColor Green
Write-Host "运行批量生成: python branch_7/batch.py --topics topics.txt --render" -ForegroundColor Green
Write-Host "======================================" -ForegroundColor Green
