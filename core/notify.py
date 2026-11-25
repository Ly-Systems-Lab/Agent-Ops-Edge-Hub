#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台级通知器 —— **以「派派」身份**把 hub/alert/# 送到企微。

## 为什么有这个东西（2026-11-25 用户定案）

    「一切皆插件的原则下，主动推送不应绑定 C3 小电视身份，
      应该用派派 Agent 的身份来发送。」

改之前：`devices/smalltv/bot.py` 订阅 `hub/alert/#` 并推企微。三个实际问题 ——

1. **C3 插件成了核心功能的强制依赖。** 停掉 `pihub-wecom-bot`，
   涨跌停告警就整个失效 —— 而用户根本不需要小电视在线。
2. **身份错认。** 收件人看到发送者是「C3 小电视」，内容（茅台涨停、杭州天气）
   却跟电视毫无关系。
3. **违反分层。** `hub/alert/*` 是平台级主题（名字里没任何设备痕迹），
   不该由 `devices/*` 里的某个设备插件消费。

改之后：本模块是 `hub/alert/#` **唯一的消费者**，走派派通道。
C3 插件退回纯设备角色（只收 `hub/command/smalltv`）。

## 发送通路：为什么调 openclaw CLI 而不是自己连 WS

派派是 OpenClaw 的 wecom 通道（`botId=aiboDee3y…`）。它有自己的 WS 长连接，
**已经在 gateway 进程里跑着**。自己再拉一条 WS 会有两个隐患：
① 企微智能机器人同一 bot 多连接会互踢（`already connected, closing old connection`）；
② 要把 secret 复制进 pihub/.env，凭据多处散落。

调 `openclaw message send` 则复用派派既有连接：
**不需要复制任何凭据，也不需要重启 gateway。**

## ★★★★★ 踩坑记录：--target 必须是裸 chatid

```
openclaw message send --channel wecom --target "group:wrln…iTw"   → 93006 invalid chatid ❌
openclaw message send --channel wecom --target "wrln…iTw"         → ✅ Sent
```

插件的 outbound 只做 `to.replace(/^wecom:/i, "")`，**不解析 `group:` / `user:` 前缀**，
会把它当字面 chatid 发给企微。所以这里必须传裸 chatid。
（`user:<EXAMPLE_USERID>` 同理，单聊也用不了 —— 智能机器人**单聊只有被动回复，
入站消息不带 chatid**，实测入站体是 `{"chattype":"single","from":{"userid":"<EXAMPLE_USERID>"}}`，
没有 chatid 字段 ⇒ 单聊无法主动推送。群聊才有 chatid。）

## 配法

1. `.env` 里写 `PIHUB_NOTIFY_CHATID=wrln…`（**裸 chatid，不带前缀**）
2. 派派必须在该群里，且**在群里发过至少一条消息**（否则企微侧没有这个会话）
3. systemd 跑 `pihub-notify`

自测：
    python3 core/notify.py --send-test "内容"
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import paho.mqtt.client as mqtt

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from core.config import get as cfg
from core import mqtt_guard

BASE_DIR = BASE
LOG_DIR = os.path.join(PROJECT, "logs")

# 派派的通道与推送目标群（chatid 必须裸值）
CHANNEL = "wecom"
OPENCLAW_BIN = os.environ.get("PIHUB_OPENCLAW_BIN") or shutil.which("openclaw") or "openclaw"

# 发消息超时（秒）。openclaw CLI 一次往返实测 0.3~1.5s，给足余量
SEND_TIMEOUT = 25

_log_fh = None


def log(msg):
    """写日志。

    ★ 只写文件，不 print（2026-11-25）：systemd 单元里已经是
      StandardOutput=append:logs/notify.log，再 print 会经 journald 重定向
      写回**同一个文件** ⇒ 每条日志出现两遍。
      没有文件句柄时（本机直接跑、--send-test）才 print。
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if _log_fh is not None:
        try:
            _log_fh.write(line + "\n")
            _log_fh.flush()
        except Exception:
            pass
    else:
        print(line, flush=True)


# ---------------- 渲染（从 devices/smalltv/bot.py 搬过来） ----------------
ALERT_TITLES = {
    "limit_up": "涨停警报",
    "limit_down": "跌停警报",
    "surge": "异动提醒",
    "weather_warning": "天气预警",
}

STOCK_TYPES = ("limit_up", "limit_down", "surge")
# 定时快照（现价 / 天气）：payload = {"key", "markdown"}，正文已渲染好
SNAPSHOT_TYPES = ("oil_price", "hangzhou_weather")


def _fmt_missing(v, suffix=""):
    """数值字段的展示兜底：None / 空串 / 非法值 → 「（数据缺失）」。

    ★ 不能用 :g —— 它会把 1688.0 显示成 1688，悄悄改变已验收的格式（2026-10-31 教训）。
    """
    if v is None or v == "":
        return "（数据缺失）"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return f"（数据缺失：{v}）"
    s = f"{f:.2f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return s + suffix


def format_alert(topic, payload):
    """MQTT 主题+payload → 企微 markdown 文本。"""
    from datetime import datetime

    atype = topic.rsplit("/", 1)[-1] if "/" in topic else topic
    title = ALERT_TITLES.get(atype, "提醒")
    sym = payload.get("symbol", "") or ""
    label = payload.get("label") or sym
    code = sym[2:] if len(sym) > 2 and sym[:2] in ("sh", "sz") else sym

    if atype == "weather_warning":
        return (f"**⚠️ {title}**\n"
                f"> 城市：{payload.get('city','')}\n"
                f"> 等级：{payload.get('level','')}\n"
                f"> {payload.get('text','')}")

    # ★ 2026-10-31：只有确定是股票类告警才走股票模板。
    #   原来用「不是 weather_warning ⇒ 股票」，任何未知主题都会被渲染成
    #   「现价 ¥None 涨幅 +None%」—— 长得像告警，实际全是空值。这里改白名单。
    if atype in STOCK_TYPES:
        price = payload.get("price")
        pct = payload.get("pct")
        sign = ""
        try:
            sign = "+" if float(pct) >= 0 else ""
        except (TypeError, ValueError):
            sign = ""
        head = f"> {label}({code}) " if sym else "> （未提供标的）"
        lines = [f"**{title}**", head.rstrip(),
                 f"> 现价 ¥{_fmt_missing(price)}　涨幅 {sign}{_fmt_missing(pct, '%')}"]
        if atype == "surge" and payload.get("threshold") is not None:
            th = payload["threshold"]
            try:
                th_s = f"{float(th):g}"
            except (TypeError, ValueError):
                th_s = str(th)
            lines.append(f"> 触发阈值：±{th_s}%")
        lines.append(f"> {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    # 定时快照：monitor 已渲染好正文，原样发出
    if atype in SNAPSHOT_TYPES:
        body = payload.get("markdown")
        if body:
            return str(body)
        log(f"[snapshot] {topic} 缺 markdown 字段，降级为通用兜底")

    # 通用兜底：认不出的类型不猜语义，原样陈述，绝不拼 None
    lines = [f"**{title}**", f"> 类型：{atype}"]
    if payload.get("raw") is not None:
        raw = str(payload.get("raw"))[:200]
        lines.append("> ⚠️ 原文（非 JSON 对象，未解析）：")
        lines.append(f"> {raw}")
    else:
        for k in ("symbol", "label", "price", "pct", "threshold", "city", "level", "text"):
            if payload.get(k) is not None:
                lines.append(f"> {k}：{payload[k]}")
    if len(lines) == 2:
        lines.append("> （无可展示字段）")
    return "\n".join(lines)


# ---------------- 发送 ----------------
def chatid():
    """派派的推送目标群。**裸 chatid，不带 group: 前缀**（见模块顶部踩坑记录）。"""
    v = (os.environ.get("PIHUB_NOTIFY_CHATID") or cfg("notify_chatid") or "").strip()
    return v


def send_markdown(content):
    """以派派身份发 markdown 到群。返回 (ok, 说明)。

    ★ 判据：**看 CLI 退出码 + stdout 里的 "✅ Sent"**。
      不能只看退出码 —— openclaw 在投递失败时会以 GatewayClientRequestError
      打 stderr 并返回非 0，但历史上也有过 rc=0 却 errcode 非 0 的情况，
      所以两个都查。
    """
    target = chatid()
    if not target:
        return False, "未配置 PIHUB_NOTIFY_CHATID"
    cmd = [OPENCLAW_BIN, "message", "send",
           "--channel", CHANNEL, "--target", target, "--message", content]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"CLI 超时 {SEND_TIMEOUT}s"
    except Exception as e:
        return False, f"CLI 调用失败: {e}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode == 0 and "Sent" in out:
        return True, out[-120:]
    return False, f"rc={r.returncode} out={out[-120:]} err={err[-200:]}"


# ---------------- MQTT ----------------
def make_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pihub-notify")

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log(f"[mqtt] 连接失败 rc={reason_code}")
            return
        c.subscribe("hub/alert/#", qos=1)
        log("[mqtt] 已连接，订阅 hub/alert/#")

    def on_message(client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"raw": msg.payload.decode("utf-8", "ignore")}
        if not isinstance(payload, dict):
            log(f"[mqtt] {topic} 不是 JSON 对象，按原文处理")
            payload = {"raw": msg.payload.decode("utf-8", "ignore")}
        content = format_alert(topic, payload)
        ok, info = send_markdown(content)
        if ok:
            log(f"[mqtt] {topic} -> 已推送 {info}")
        else:
            log(f"[mqtt] {topic} -> 推送失败 {info}")

    client.on_connect = on_connect
    # 兜底边界：漏网的异常不许冲出回调，否则 loop_start 的网络线程会静默退出
    client.on_message = mqtt_guard.guard(on_message, log)
    host = cfg("mqtt_host") or "127.0.0.1"
    port = int(cfg("mqtt_port") or 1883)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client


def main():
    global _log_fh
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", metavar="TEXT", help="直接推一条测试，不进 MQTT")
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    _log_fh = open(os.path.join(LOG_DIR, "notify.log"), "a", encoding="utf-8")

    if args.send_test:
        ok, info = send_markdown(args.send_test)
        log(f"[test] ok={ok} {info}")
        raise SystemExit(0 if ok else 1)

    log(f"[start] notify 启动 chatid={chatid() or '(未配置)'} via={OPENCLAW_BIN}")
    client = make_client()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("[stop] 收到中断")
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        _log_fh.close()


if __name__ == "__main__":
    main()
