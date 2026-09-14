#!/usr/bin/env bash
# 启动 FastAPI（:8000）。首次启动自动建表并播种合成数据。
set -euo pipefail
source /workspace/miniforge3/etc/profile.d/conda.sh
conda activate roast
cd /workspace/backend
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
