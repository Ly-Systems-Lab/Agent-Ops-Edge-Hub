#!/usr/bin/env bash
# 基础服务检查（原「§9 整机断电重启验收」）—— Pi Hub
#
# 用法：
#   1) 断电重启前，先记录基线：  ./acceptance_s9.sh baseline
#   2) 断电 -> 重启树莓派 -> 等约 60~90s（等 lightdm 自动登录 + 用户服务拉起 + 音频就绪）
#   3) 重启后检查：               ./acceptance_s9.sh check
#
# ⚠️ 这个脚本【证明不了「整机验收通过」】，只证明【基础服务】在跑。原因：
#    它验不到「企微指令真的执行了」和「告警真的推到了手机」这两条业务链路
#    （见文件末尾的 4)/5) —— 故意不自动测，理由写在那里）。
#    因此结论按 进程 / 网络 / 设备在线 / 控制回执 / 告警送达 五档分开打印，
#    后两档恒为「未覆盖」，需人工确认。
#    旧版会把前三档通过打印成一句「结论: 通过」，容易被读成整条链路通了。
#
# ⚠️ 本脚本【并非只读】，会写这些东西（旧版注释里"不做任何写操作"是错的）：
#    ① logs/s9_acceptance.log（所有输出都 tee 进去）
#    ② logs/s9_baseline.env（仅在 baseline 子命令下）
#    ③ 向本地 Mosquitto 发一条 s9/selftest（不入 hub/alert/#，不会推到企微群）
#    ④ 设备自检里请求 /api/health（回环 + WiFi IP）—— 只读探活端点
#    ⑤ 建 logs/ 目录（若不存在）
#
# ⚠️ **设备专属的检查不在这里。** 它们在 `devices/<name>/selfcheck.sh`，
#    本脚本启动时 source 进来、再按钩子调用。核心不认识任何设备 ⇒
#    加一台设备**不用改本脚本**。钩子契约见 docs/DEVICE-PLUGIN.md「三之二」。
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_FILE="$DIR/logs/s9_baseline.env"
LOG="$DIR/logs/s9_acceptance.log"
mkdir -p "$DIR/logs"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# ---------- 服务清单：从目录扫，不写死服务名 ----------
#   核心服务 = systemd/*.service             （改它会影响所有设备）
#   设备服务 = devices/*/systemd/*.service   （一台设备一个目录）
#   ⇒ 加一台设备**不用改这个脚本** —— 这正是 docs/DEVICE-PLUGIN.md 判据 3 的要求。
#   `[!_]*` 是要跳过 devices/_template/（模板里的单元不是真服务）。

SERVICES=(mosquitto)
while IFS= read -r u; do
  [ -n "$u" ] && SERVICES+=("$(basename "$u" .service)")
done < <(ls "$DIR"/systemd/*.service 2>/dev/null)

DEVICE_SERVICES=()
while IFS= read -r u; do
  [ -n "$u" ] && DEVICE_SERVICES+=("$(basename "$u" .service)")
done < <(ls "$DIR"/devices/[!_]*/systemd/*.service 2>/dev/null)

# ---------- 设备自检钩子：devices/<name>/selfcheck.sh ----------
#   被 source 进**同一个 shell**，因此能用 log() / $DIR / $BASE_FILE / $LOG。
#   需要实现的钩子（不需要的可以不写）：
#     <name>_selfcheck_net      返回 0/1，计入「网络」档
#     <name>_selfcheck_device   返回 0/1，计入「设备在线」档
#     <name>_selfcheck_hint     不参与判据，只打提示
#     <name>_baseline           打 KEY=value 行，写进 baseline 文件
#   函数名由**目录名**推出来 ⇒ 核心里没有任何设备名。
SELFCHECK_DEVICES=()
while IFS= read -r f; do
  [ -n "$f" ] || continue
  name=$(basename "$(dirname "$f")")
  case "$name" in
    *[!a-z0-9_]*)
      log "  [WARN] devices/$name/selfcheck.sh：目录名只能是 a-z0-9_，已跳过"
      continue
      ;;
  esac
  # shellcheck source=/dev/null
  . "$f"
  SELFCHECK_DEVICES+=("$name")
done < <(ls "$DIR"/devices/[!_]*/selfcheck.sh 2>/dev/null)

has_hook() { declare -F "$1" >/dev/null 2>&1; }

check_services() {
  local fail=0
  for s in "${SERVICES[@]}"; do
    st=$(systemctl is-active "$s" 2>/dev/null || true)
    en=$(systemctl is-enabled "$s" 2>/dev/null || true)
    if [ "$st" = "active" ] && [ "$en" = "enabled" ]; then
      log "  [OK]   $s  active/enabled"
    else
      log "  [FAIL] $s  active=$st enabled=$en"
      fail=1
    fi
  done
  return $fail
}

# 设备服务：没装/没启用 = 正常（不需要那台设备）；启用了却没起来 = 失败。
# 判据用的是 is-enabled —— "装了并期望它跑"是用户在 systemd 里表达过的意图。
check_device_services() {
  local fail=0
  for s in "${DEVICE_SERVICES[@]}"; do
    st=$(systemctl is-active "$s" 2>/dev/null || true)
    en=$(systemctl is-enabled "$s" 2>/dev/null || true)
    if [ "$en" = "not-found" ] || [ -z "$en" ]; then
      log "  [INFO] $s  未安装（不需要这台设备就不用装）"
    elif [ "$en" = "disabled" ]; then
      log "  [INFO] $s  未启用（不参与自启）"
    elif [ "$st" = "active" ]; then
      log "  [OK]   $s  active/enabled（设备服务）"
    else
      log "  [FAIL] $s  active=$st enabled=$en（已启用却没起来：查 .env 里的凭据键）"
      fail=1
    fi
  done
  return $fail
}

# ---------- Mosquitto 不对外 ----------
check_mqtt() {
  local fail=0
  local bind bad
  bind=$(ss -ltn 2>/dev/null | awk '$4 ~ /:1883$/ {print $4}')
  # 必须是【且只能是】回环。旧版写成 `grep -q "127.0.0.1:1883"`，只要输出里
  # 【任意一行】含它就判 OK —— 于是 0.0.0.0:1883 与 127.0.0.1:1883 并存时照样通过，
  # 等于放过了对公网暴露。这里改成"白名单之外的非空行即为 FAIL"。
  bad=$(echo "$bind" | grep -vE '^(\[::1\]|127\.0\.0\.1):1883$' | grep -v '^$' || true)
  if echo "$bind" | grep -qE '^(\[::1\]|127\.0\.0\.1):1883$'; then
    if [ -z "$bad" ]; then
      log "  [OK]   Mosquitto 仅绑回环 ($(echo $bind | tr '\n' ' '))"
    else
      log "  [FAIL] Mosquitto 另有对外监听: $(echo $bad | tr '\n' ' ') —— 应只留回环"
      fail=1
    fi
  else
    log "  [FAIL] Mosquitto 未绑回环 1883 (实际: '${bind:-无监听}')"
    fail=1
  fi
  # 回路自测（发到 s9/selftest，Bot 只订阅 hub/alert/# 和指令回执，不会推到企微群）
  if timeout 5 mosquitto_pub -h 127.0.0.1 -t 's9/selftest' -m 'ping' 2>/dev/null; then
    log "  [OK]   mosquitto_pub 回路可用"
  else
    log "  [FAIL] mosquitto_pub 失败"; fail=1
  fi
  return $fail
}

case "${1:-check}" in
  baseline)
    {
      echo "WLAN_IP=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)"
      echo "TS=$(date +%s)"
      for d in "${SELFCHECK_DEVICES[@]}"; do
        has_hook "${d}_baseline" && "${d}_baseline"
      done
    } > "$BASE_FILE"
    log "=== 基线已记录 -> $BASE_FILE ==="
    cat "$BASE_FILE" | tee -a "$LOG"
    ;;
  check|*)
    log "======== 基础服务检查 $(date '+%F %T') ========"
    rc=0; v_proc="通过"; v_net="通过"; v_dev="通过"
    log "-- 1) 进程：核心服务 + 设备服务（都从目录扫，不写死服务名）--"
    check_services || { rc=1; v_proc="失败"; }
    check_device_services || { rc=1; v_proc="失败"; }
    log "-- 2) 网络：设备端点 + Mosquitto 边界 --"
    for d in "${SELFCHECK_DEVICES[@]}"; do
      has_hook "${d}_selfcheck_net" && { "${d}_selfcheck_net" || { rc=1; v_net="失败"; }; }
    done
    check_mqtt  || { rc=1; v_net="失败"; }
    log "-- 3) 设备在线：各设备自检 --"
    v_dev_checked=0
    for d in "${SELFCHECK_DEVICES[@]}"; do
      if has_hook "${d}_selfcheck_device"; then
        v_dev_checked=1
        "${d}_selfcheck_device" || { rc=1; v_dev="失败"; }
      fi
    done
    if [ "$v_dev_checked" -eq 0 ]; then
      v_dev="未覆盖"
      log "  [INFO] 没有设备提供在线检查（devices/<name>/selfcheck.sh 的 <name>_selfcheck_device）"
    fi
    log "-- 4) 控制回执：本脚本不覆盖 --"
    log "  [SKIP] 需人工：在企微群 @机器人 发一条指令（如「亮度60」），看是否回「已执行」"
    log "-- 5) 告警送达：本脚本不覆盖 --"
    log "  [SKIP] 需人工：确认企微群能收到阈值提醒"
    log "  [WHY]  不自动测：Bot 订阅 hub/alert/#，脚本一旦伪造告警就会真的推到用户手机上"
    log "-- 附) 换段提示 --"
    for d in "${SELFCHECK_DEVICES[@]}"; do
      has_hook "${d}_selfcheck_hint" && "${d}_selfcheck_hint"
    done

    log "======== 结论（五档分开，勿合并读）========"
    log "  [$v_proc] 进程       — 核心服务 + 已启用的设备服务 active+enabled"
    log "  [$v_net] 网络       — 设备端点 + Mosquitto 只绑回环"
    log "  [$v_dev] 设备在线   — 各设备自检（连通 ≠ 显示对 ≠ 能收指令）"
    log "  [未覆盖] 控制回执   — 需人工，见上 4)"
    log "  [未覆盖] 告警送达   — 需人工，见上 5)"
    if [ $rc -eq 0 ]; then
      log "==== 基础服务：通过（业务链路未验证）===="
    else
      log "==== 基础服务：有 FAIL，见上 ===="
    fi
    exit $rc
    ;;
esac