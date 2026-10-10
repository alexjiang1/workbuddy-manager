#!/usr/bin/env bash
# WorkBuddy Manager - macOS / Linux Native Stop Script
PORT="${WB_MANAGER_PORT:-7864}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "正在停止 WorkBuddy Manager (端口 $PORT)..."
pkill -f "uvicorn server.main:app" 2>/dev/null || true
echo "WorkBuddy Manager 管理端已停止。"

if [ -x "$ROOT/upstream/stop-workbuddy2api.sh" ]; then
  "$ROOT/upstream/stop-workbuddy2api.sh"
fi
