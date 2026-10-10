#!/usr/bin/env bash
# WorkBuddy Manager - macOS / Linux Native Start Script
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

HOST="${WB_MANAGER_HOST:-127.0.0.1}"
PORT="${WB_MANAGER_PORT:-7864}"
PYTHON="$ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "[ERROR] Python 虚拟环境不存在于 $PYTHON"
  exit 1
fi

# 启动上游核心网关服务
if [ -x "$ROOT/upstream/start-workbuddy2api.sh" ]; then
  "$ROOT/upstream/start-workbuddy2api.sh"
fi

echo "=================================================="
echo " Starting WorkBuddy Manager at http://${HOST}:${PORT}"
echo "=================================================="

exec "$PYTHON" -m uvicorn server.main:app --env-file .env --host "$HOST" --port "$PORT"
