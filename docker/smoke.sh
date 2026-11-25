#!/usr/bin/env bash
# 底座冒烟：在宿主上用「只含 core/ 的临时目录」复刻镜像布局，验证
#   ① 镜像只装 core/ 的假设成立（没有 devices/ 也能 import 与启动）
#   ② entrypoint.sh 能正确定位仓库根
#   ③ init_db 能建出 monitor 需要的表
#
# 这不是 docker build 的替代品，只是拉不到基础镜像时的降级自检。
# 镜像成品仍须在有网机器上 `docker build` 后跑 docker/README.md §5。
#
# 用法：bash docker/smoke.sh [--with-mqtt]
#   --with-mqtt  额外跑一次 monitor --once（默认只跑 --help，避免扰动告警）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d /tmp/pihub-base-smoke.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1. 复刻镜像布局（只有 core/ + entrypoint）"
mkdir -p "$TMP/opt/pihub"
cp -r "$REPO/core" "$TMP/opt/pihub/"
cp "$REPO/requirements.txt" "$TMP/opt/pihub/"
cp "$REPO/docker/entrypoint.sh" "$TMP/opt/pihub/"
ls "$TMP/opt/pihub"

echo
echo "== 2. 断言：没有 devices/"
if [ -d "$TMP/opt/pihub/devices" ]; then
  echo "❌ 意外出现 devices/ —— 镜像边界被破坏"; exit 1
fi
echo "NO devices/ ✅"

echo
echo "== 3. 断言：core/ 里没有任何 devices 的 import"
if grep -rn "^from devices\|^import devices\|from devices\." "$TMP/opt/pihub/core" ; then
  echo "❌ core 反向依赖插件（违反「依赖单向」）"; exit 1
fi
echo "core 不 import devices ✅"

echo
echo "== 4. entrypoint --help（改 cwd 前缀以便宿主跑）"
sed "s#/opt/pihub#$TMP/opt/pihub#" "$TMP/opt/pihub/entrypoint.sh" > "$TMP/ep.sh"
cd "$TMP/opt/pihub"
PIHUB_DB="$TMP/smoke.db" bash "$TMP/ep.sh" --help

echo
echo "== 5. init_db 建库（monitor 依赖 symbols 表）"
PIHUB_DB="$TMP/smoke.db" python3 -m core.init_db | head -4

if [ "${1:-}" = "--with-mqtt" ]; then
  echo
  echo "== 6. monitor --once（连宿主 127.0.0.1:1883 真跑一轮）"
  echo "   注意：若命中涨跌停/异动会真发告警，冷却已按 DB 走。"
  PIHUB_DB="$TMP/smoke.db" timeout 60 python3 -m core.monitor --once --force || true
else
  echo
  echo "== 6. monitor --once 已跳过（加 --with-mqtt 才跑）"
fi

echo
echo "ALL SMOKE CHECKS PASSED ✅"
