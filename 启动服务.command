#!/bin/bash
# 砍树勘察作业管理 · 本地启动
# 双击本文件即可启动服务并自动打开浏览器（关闭此终端窗口即停止服务）
cd "$(dirname "$0")"

# 优先用 WorkBuddy 内置 python，其次用系统 python3
PY="/Users/kimho/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "❌ 找不到 python3，请先安装 Python 3"
  read -n 1 -s -r -p "按任意键退出…"
  exit 1
fi

# 从 .env 取端口（默认 8000）；若被占用则自动换 8010
PORT="$(grep -E '^PORT=' .env 2>/dev/null | head -1 | cut -d= -f2)"
PORT="${PORT:-8000}"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "⚠️  端口 $PORT 已被占用，改用 8010"
  PORT=8010
fi

echo "======================================"
echo "  砍树勘察作业管理 · 本地服务"
echo "  地址：http://127.0.0.1:$PORT"
echo "  关闭本窗口即停止服务"
echo "======================================"
sleep 1
open "http://127.0.0.1:$PORT" 2>/dev/null

PORT="$PORT" exec "$PY" server.py
