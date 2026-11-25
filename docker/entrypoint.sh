#!/usr/bin/env bash
# 底座镜像薄入口：参数原样转给 core/monitor.py。
#   entrypoint.sh --help     看 CLI
#   entrypoint.sh --once     跑一轮就退（自检）
#   entrypoint.sh            常驻轮询
#
# ⚠️ 必须先建库：monitor 依赖 symbols 表，空 DB 会 sqlite3.OperationalError。
#    首次启动前在容器内跑一次：python3 -m core.init_db
set -euo pipefail
cd /opt/pihub
exec python3 -m core.monitor "$@"
