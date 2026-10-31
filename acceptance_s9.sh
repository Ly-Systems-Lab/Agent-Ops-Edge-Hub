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
#    ④ 请求 /api/health（回环）—— ★ 2026-10-31 改：原为 /api/stock/sh000001，
#       该端点缓存过期时会打上游并写库；验收脚本应为纯只读，故改探活端点
#    ⑤ 请求 /api/health（WiFi IP，验白名单）—— 只读，且**不触发** smalltv_ip 学习
#    ⑥ 建 logs/ 目录（若不存在）
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_FILE="$DIR/logs/s9_baseline.env"
LOG="$DIR/logs/s9_acceptance.log"
mkdir -p "$DIR/logs"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# 用【服务自己那套取值规则】解析 smalltv_ip（env > .env > SQLite config > 默认），
# 直接复用 app/config.py，而不是自己写一条 sqlite 查询。原因：
#   README 推荐把 smalltv_ip 写进 .env，而 .env 的优先级【高于】数据库 ——
#   若脚本只查库，就会出现"控制其实完全正常、脚本却报未配置"的假告警。
# 同时尊重 PIHUB_DB 指定的自定义库路径。
get_smalltv_ip() {
  PIHUB_DB="${PIHUB_DB:-$DIR/data/pihub.db}" python3 - "$DIR" <<'PY'
import os, sys
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "app"))
try:
    from config import get
    print(get("smalltv_ip", "") or "")
except Exception as e:
    sys.stderr.write(f"[cfg] 读取 smalltv_ip 失败: {e}\n")
    print("")
PY
}

# ---------- 服务清单（5 个基础 + 2 个可选设备机器人） ----------
SERVICES=(mosquitto pihub-api pihub-monitor pihub-devctl pihub-wecom-bot)

# 设备机器人按需安装（要有对应企微凭据才会起来），所以只报状态、不计入失败
OPTIONAL_SERVICES=(pihub-p4-bot pihub-car-bot)

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

check_optional_services() {
  for s in "${OPTIONAL_SERVICES[@]}"; do
    st=$(systemctl is-active "$s" 2>/dev/null || true)
    en=$(systemctl is-enabled "$s" 2>/dev/null || true)
    if [ "$st" = "active" ] && [ "$en" = "enabled" ]; then
      log "  [OK]   $s  active/enabled（可选）"
    elif [ "$en" = "not-found" ] || [ -z "$en" ]; then
      log "  [INFO] $s  未安装（不需要该设备就不用装）"
    else
      log "  [WARN] $s  active=$st enabled=$en（装了但没起来，查 .env 里的凭据键）"
    fi
  done
  return 0
}

# ---------- Flask 绑定 & 端点 ----------
check_flask() {
  local fail=0
  local bind
  bind=$(ss -ltn 2>/dev/null | awk '$4 ~ /:5000$/ {print $4}')
  if echo "$bind" | grep -qE '^0\.0\.0\.0:5000$'; then
    log "  [OK]   Flask LISTEN 0.0.0.0:5000 (免疫换 IP)"
  else
    log "  [FAIL] Flask 绑定异常: '${bind:-无监听}' (期望 0.0.0.0:5000)"
    fail=1
  fi

  # 回环可达（devctl 走这个）
  # ★ 2026-10-31 Pi 侧改（A2）：回环探活也改打 /api/health。
  #   原为 /api/stock/sh000001 —— 该端点缓存过期时会**打上游并写库**
  #   （kline_fetch_at 时间戳被刷新）。验收脚本应当是**纯只读**的，
  #   不该每跑一次就赌一次缓存是否过期。数据端点可用性由 check_device 覆盖。
  code=$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/api/health || echo 000)
  [ "$code" = "200" ] && log "  [OK]   curl 127.0.0.1:5000/api/health -> 200" || { log "  [FAIL] 回环端点 -> $code"; fail=1; }

  # WiFi IP 可达 —— ★ 这一项现在会真的受 http_allow 约束
  # （从本机 curl 自己的 wlan IP，源地址就是 wlan IP，不是回环）
  #
  # ★ 2026-10-31：这里改用 /api/health 而不是 /api/stock/...
  #   原因：数据端点会触发「被动学习 smalltv_ip」，而本机自访问的源地址正是
  #   Pi 的 WiFi IP —— 一旦白名单按网段放行，跑一次检查就会把 smalltv_ip
  #   改写成 Pi 自己（详见 app/api.py 里的说明）。探活端点不参与学习，天然干净。
  #   数据端点本身已经由上面的「回环可达」那一项覆盖。
  ip=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)
  if [ -n "$ip" ]; then
    code=$(curl -s -m5 -o /dev/null -w '%{http_code}' "http://$ip:5000/api/health" || echo 000)
    if [ "$code" = "200" ]; then
      log "  [OK]   curl $ip:5000/api/health -> 200"
    elif [ "$code" = "403" ]; then
      log "  [FAIL] WiFi IP 端点 -> 403：$ip 不在 http_allow 白名单里"
      log "         ⇒ 修法：.env 的 http_allow 追加 $ip（**保留原有条目**，别整行替换）后重启 pihub-api"
      fail=1
    elif [ "$code" = "404" ]; then
      log "  [FAIL] WiFi IP 端点 -> 404：/api/health 不存在（api.py 版本旧于本次修复？）"
      fail=1
    else
      log "  [FAIL] WiFi IP 端点 -> $code"
      fail=1
    fi
    log "  [INFO] 当前 WiFi IP = $ip"

    # ★ 2026-10-31 回归检查：smalltv_ip 绝不能等于 Pi 自己的地址。
    #   历史上有两个入口会造成这种污染（回环、本机 WiFi IP），
    #   两者症状一样：所有控制指令被转发回本机，C3 侧表现为"没反应"。
    #
    # ★★ 2026-10-31 二次修正：**改用 /api/netinfo 的本机 IP 集合**，不再只比 wlan0。
    #   原因：api.py 的被动学习用 `_local_ipv4_set()`（主机名解析 + UDP 默认出口探测）
    #   判定"是不是 Pi 自己"，而本脚本原来用 `ip addr show wlan0` —— **两者不同源**。
    #   Pi 同时接 eth0 + wlan0（或 hostname 解析到 eth0）时，脚本只比 wlan0，
    #   会漏判「smalltv_ip 被写成 Pi 的 eth0 地址」这种同样致命的污染。
    #   现在直接问 API 要它自己的判定，脚本不再自建口径。
    cur_smalltv=$(get_smalltv_ip)
    netinfo=$(curl -s -m5 "http://127.0.0.1:5000/api/netinfo" 2>/dev/null || true)
    # ★ 判"接口可用"不能只看 body 空不空：旧版 api.py 会回一条 **404 HTML**，
    #   body 非空但解析不出 JSON ⇒ 必须**以解析结果为准**，否则降级分支永远不触发，
    #   老版本上这一项会静默变成"永远 INFO/通过"（比不检查更糟）。
    parsed=$(printf '%s' "$netinfo" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict) or "polluted" not in d:
        raise ValueError("not netinfo json")
except Exception:
    print("ERR")
    raise SystemExit
print("OK|%s|%s" % (d.get("polluted"), ",".join(d.get("local_ips") or [])))
' 2>/dev/null || echo ERR)

    if [ "${parsed:0:3}" = "OK|" ]; then
      rest=${parsed#OK|}
      polluted=${rest%%|*}
      local_ips=${rest#*|}
      if [ "$polluted" = "True" ] || [ "$polluted" = "true" ]; then
        log "  [FAIL] smalltv_ip = ${cur_smalltv} 落在【Pi 自己的地址集合】里！"
        log "         本机 IP: ${local_ips:-<未知>}"
        log "         ⇒ 所有控制指令都会打回本机。请改回设备真实 IP 后重启 pihub-api。"
        fail=1
      else
        log "  [INFO] smalltv_ip = ${cur_smalltv:-<空>}（不在本机地址集合内：${local_ips:-<未知>}）"
      fi
    else
      log "  [INFO] /api/netinfo 不可用（旧版 api.py？），回退为只比 wlan0 IP"
      if [ -n "$cur_smalltv" ] && [ "$cur_smalltv" = "$ip" ]; then
        log "  [FAIL] smalltv_ip = $ip —— 这是【Pi 自己的 WiFi 地址】，不是小电视！"
        log "         ⇒ 所有控制指令都会打回本机。请改回设备真实 IP 后重启 pihub-api。"
        fail=1
      else
        log "  [INFO] smalltv_ip = ${cur_smalltv:-<空>}（不同于 Pi 自身地址）"
      fi
    fi
  else
    log "  [FAIL] wlan0 无 IPv4 地址"; fail=1
  fi
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

# ---------- ESP32 是否在线 ----------
# 注意：本函数只回答"设备在不在网、有没有在向 Pi 取数"，回答不了"指令能不能执行到位"。
check_device() {
  local fail=0
  local smalltv
  # 取值规则见上面 get_smalltv_ip()（env > .env > SQLite config > 默认）
  smalltv=$(get_smalltv_ip)
  log "  [INFO] smalltv_ip = ${smalltv:-<空>}   (env > .env > SQLite > 默认；库=${PIHUB_DB:-<默认 data/pihub.db>})"

  if [ -z "$smalltv" ]; then
    log "  [FAIL] smalltv_ip 未配置 —— 屏幕可能能显示，但企微「控制指令」必然失败(502)"
    log "         （固件不会自动上报 IP，需手工填：README 第三步 ①）"
    return 1
  fi

  if ping -c1 -W2 "$smalltv" >/dev/null 2>&1; then
    log "  [OK]   ESP32 $smalltv 可 ping 通"
    # 近 300 行日志里 ESP32 是否访问过 Pi
    n=$(tail -300 "$DIR/logs/api.log" 2>/dev/null | grep -c "$smalltv" || true)
    if [ "$n" -gt 0 ]; then
      log "  [OK]   ESP32 近期有访问 Pi ($n 条日志)"
    else
      # 只报信息，不做判据：设备在时钟模式/相册模式时本来就不取数，无法据此判定故障
      log "  [INFO] 近期未见 ESP32 访问 Pi（可能在时钟/相册模式，也可能确实没在取数）"
    fi
  else
    log "  [FAIL] ESP32 $smalltv ping 不通"
    log "         （若设备确实已断电/未上电，此项可忽略；否则查设备是否换了 IP——"
    log "          固件不会自动上报新 IP，换网络后需手工更新 smalltv_ip）"
    fail=1
  fi
  return $fail
}

# ---------- 换段复验提示 ----------
check_swap_hint() {
  local ip
  ip=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)
  if [ -f "$BASE_FILE" ]; then
    old=$(grep '^WLAN_IP=' "$BASE_FILE" | cut -d= -f2)
    if [ -n "$old" ] && [ "$old" != "$ip" ]; then
      log "  [INFO] 检测到换段: $old -> $ip"
      log "  [INFO] Pi 侧应 0 改动自愈；ESP32 侧需改 piHubHost(网页) 并更新 Pi 的 smalltv_ip"
    else
      log "  [INFO] 网段未变 ($ip)"
    fi
  else
    log "  [INFO] 无基线文件，跳过换段对比（可先跑 ./acceptance_s9.sh baseline）"
  fi
}

case "${1:-check}" in
  baseline)
    {
      echo "WLAN_IP=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)"
      echo "TS=$(date +%s)"
      echo "API_LOG_LINES=$(wc -l < "$DIR/logs/api.log" 2>/dev/null || echo 0)"
    } > "$BASE_FILE"
    log "=== 基线已记录 -> $BASE_FILE ==="
    cat "$BASE_FILE" | tee -a "$LOG"
    ;;
  check|*)
    log "======== 基础服务检查 $(date '+%F %T') ========"
    rc=0; v_proc="通过"; v_net="通过"; v_dev="通过"
    log "-- 1) 进程：5 个基础服务自启（+ 2 个可选设备机器人，仅报状态）--"
    check_services || { rc=1; v_proc="失败"; }
    check_optional_services
    log "-- 2) 网络：Flask 绑定/端点 + Mosquitto 边界 --"
    check_flask || { rc=1; v_net="失败"; }
    check_mqtt  || { rc=1; v_net="失败"; }
    log "-- 3) 设备在线：ESP32 可达性 --"
    check_device || { rc=1; v_dev="失败"; }
    log "-- 4) 控制回执：本脚本不覆盖 --"
    log "  [SKIP] 需人工：在企微群 @机器人 发一条指令（如「亮度60」），看是否回「已执行」"
    log "-- 5) 告警送达：本脚本不覆盖 --"
    log "  [SKIP] 需人工：确认企微群能收到阈值提醒"
    log "  [WHY]  不自动测：Bot 订阅 hub/alert/#，脚本一旦伪造告警就会真的推到用户手机上"
    log "-- 附) 换段提示 --"
    check_swap_hint

    log "======== 结论（五档分开，勿合并读）========"
    log "  [$v_proc] 进程       — systemd 5 服务 active+enabled"
    log "  [$v_net] 网络       — Flask 端点 + Mosquitto 只绑回环"
    log "  [$v_dev] 设备在线   — ESP32 可 ping 通（≠ 能显示对、≠ 能收指令）"
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
