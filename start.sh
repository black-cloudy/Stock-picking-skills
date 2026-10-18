#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  StockAgent - A股实时行情服务"
echo "============================================"

if ! python -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
  echo "[1/2] 安装依赖..."
  python -m pip install -r requirements.txt
fi

echo "[2/2] 启动服务..."
exec python run.py "$@"
