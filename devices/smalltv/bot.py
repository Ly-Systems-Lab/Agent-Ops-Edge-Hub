#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小电视（C3）企微机器人 —— 长连接 API 模式。

本文件 = 通用基类（core/wecom_base.py）的**小电视实现**：

  - 指令：企微消息 → hub/command/smalltv → devctl 执行 → ack 回群
  - 告警：订阅 hub/alert/# → 渲染成 markdown → 推到会话

基类提供 WS 长连接 / 心跳 / 重连退避 / 回执配对 / chatid 持久化；
本文件只放小电视自己的东西：主题、指令表接线、回执与告警的渲染、日志落点。

运行：<部署目录>/.venv/bin/python devices/smalltv/bot.py
自测：--send-test "内容" 直接向已记录 chatid 推一条测试（不起 MQTT）。
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime

# —— 把仓库根放进 sys.path，core.* / devices.* 才 import 得到 ——
_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "core")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        raise SystemExit(f"找不到仓库根：{__file__} 之上没有 core/")
    _p = _parent
if _p not in sys.path:
    sys.path.insert(0, _p)
import websocket  # websocket-client
import paho.mqtt.client as mqtt
from core.config import get as cfg
from core.wecom_base import WeComBot, WS_URL, _log
from core import mqtt_guard
from devices.smalltv.command import parse_command, describe, HELP_TEXT

# 告警类型 → 中文标题
ALERT_TITLES = {
    "limit_up": "涨停警报",
    "limit_down": "跌停警报",
    "surge": "异动提醒",
    "weather_warning": "天气预警",
}

# §7.2 指令总线（方案 A：企微指令必走 MQTT）
CMD_TOPIC = "hub/command/smalltv"
CMD_ACK_TOPIC = "hub/command/smalltv/ack"


# ---------------- 消息格式化 ----------------
STOCK_TYPES = ("limit_up", "limit_down", "surge")


def _fmt_missing(v, suffix=""):
    """数值字段的展示兜底。None / 空串 / 非法值 → 「（数据缺失）」。

    ★ 为什么要有（2026-10-31 修）：
      原来直接 f"¥{price}"，而 price 取不到时是 None ⇒ 群里收到「现价 ¥None」。
      None 不会报错，只会静默地把「没数据」伪装成「数据是 None」，最容易被当真的。
    """
    if v is None or v == "":
        return "（数据缺失）"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    # 保持与旧版一致的展示：1688.00 → 1688.0、3 → 3.0、4.12 → 4.12
    # 不能用 :g —— 它会把 1688.0 显示成 1688，悄悄改变了已验收的格式
    s = f"{f:.2f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return s + suffix


def format_alert(topic, payload):
    """MQTT 主题+payload → 企微 markdown 文本（§7.2 示例风格）。"""
    atype = topic.rsplit("/", 1)[-1] if "/" in topic else topic
    title = ALERT_TITLES.get(atype, "提醒")
    sym = payload.get("symbol", "") or ""
    label = payload.get("label") or sym
    # 代码：sh600519 -> 600519
    code = sym[2:] if len(sym) > 2 and sym[:2] in ("sh", "sz") else sym

    if atype == "weather_warning":
        return (f"**⚠️ {title}**\n"
                f"> 城市：{payload.get('city','')}\n"
                f"> 等级：{payload.get('level','')}\n"
                f"> {payload.get('text','')}")

    # ★ 2026-10-31：只有确定是股票类告警才走股票模板。
    #   原来用「不是 weather_warning ⇒ 股票」，于是任何**未知主题**
    #   （hub/alert/xxx 拼错、带了子路径、将来新增类型）都会被渲染成
    #   「> ()\n> 现价 ¥None 涨幅 +None%」—— 长得像股票告警，实际全是空值。
    #   这里改成白名单：认不出就走通用兜底，把 payload 原样列出。
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
            # 阈值单独格式化：不带 .0（±3% 而非 ±3.0%），与旧版展示保持一致
            th = payload["threshold"]
            try:
                th_s = f"{float(th):g}"
            except (TypeError, ValueError):
                th_s = str(th)
            lines.append(f"> 触发阈值：±{th_s}%")
        lines.append(f"> {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    # 通用兜底：未知告警类型，不猜语义，原样陈述，绝不拼 None
    lines = [f"**{title}**", f"> 类型：{atype}"]
    if payload.get("raw") is not None:
        # 上游 JSON 解析失败时 on_message 会塞成 {"raw": 原文}
        raw = str(payload.get("raw"))[:200]
        lines.append("> ⚠️ 原文（非 JSON 对象，未解析）：")
        lines.append(f"> {raw}")
    else:
        for k in ("symbol", "label", "price", "pct", "threshold", "city", "level", "text"):
            if payload.get(k) is not None:
                lines.append(f"> {k}：{payload[k]}")
    lines.append(f"> {datetime.now().strftime('%H:%M:%S')}")
    return "\n".join(lines)


# ---------------- 小电视机器人 ----------------
class SmallTvBot(WeComBot):
    """C3 小电视：认「开/关/亮度/音量/图片…」那套指令，告警渲染成股票模板。"""

    # ★ 本设备的推送目标键。小电视是最早的那台，用的是不带后缀的一组
    #   （P4 / 小车用的是 wecom_chat_id_p4 / _car）。
    TARGET_KEY = "wecom_chat_id"
    TYPE_KEY = "wecom_chat_type"

    def send_command(self, action, value, response_url=None, extra=None):
        """发指令到 MQTT 总线 hub/command/smalltv（§7.2 方案 A：不自调 API）。
        等待 devctl 执行后回 ack，再用 response_url 回群。
        """
        req_id = self._new_req_id()
        if response_url:
            with self._pending_lock:
                # 记「发起时刻」供超时判定，记「业务 ID」让超时回报也能对上发起方的步骤
                self.pending[req_id] = (response_url, time.time(),
                                        (extra or {}).get("tid"))
        payload = {"action": action, "value": value, "req_id": req_id}
        if extra:
            payload["extra"] = extra
        if self.mqtt is None:
            _log("[cmd] MQTT 未连接，无法发指令")
            if response_url:
                self.reply(response_url, "指令总线未连接，请稍后重试")
            return False
        self.mqtt.publish(CMD_TOPIC, json.dumps(payload, ensure_ascii=False), qos=1)
        _log(f"[cmd] publish {CMD_TOPIC} {payload}")
        return True

    # ---------- 收到用户文本：小电视的指令入口 ----------
    def on_text(self, body, text):
        """解析指令 → 发 MQTT 总线 → devctl 执行 → ack 回群。"""
        rurl = body.get("response_url")
        action, value, extra = parse_command(text)
        if action and action != "help":
            self.send_command(action, value, response_url=rurl, extra=extra)
        elif action == "help":
            # help 不走 MQTT，所以业务 ID 要在这里自己带上（与其他回执保持一致）
            tid = (extra or {}).get("tid")
            self.reply(rurl, f"[{tid}]\n{HELP_TEXT}" if tid else HELP_TEXT)
        else:
            # 未识别为指令：回一句引导（同时便于排障确认消息已到达）
            self.reply(rurl, "收到～\n" + HELP_TEXT)
            if self.on_message:
                try:
                    self.on_message(body)
                except Exception as e:
                    _log(f"[ws] on_message 异常: {e}")

# ---------------- MQTT 订阅 ----------------
def start_mqtt(bot, log_path):
    host = cfg("mqtt_host") or "127.0.0.1"
    port = int(float(cfg("mqtt_port") or 1883))

    def on_connect(client, userdata, flags, rc, props=None):
        client.subscribe("hub/alert/#", qos=1)
        client.subscribe(CMD_ACK_TOPIC, qos=1)
        _log(f"[mqtt] connected {host}:{port}, subscribed hub/alert/# + {CMD_ACK_TOPIC}")

    def on_message(client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"raw": msg.payload.decode("utf-8", "ignore")}
        if not isinstance(payload, dict):
            # 合法 JSON 但不是对象（[] / 123 / "x"）⇒ format_alert 里的 .get() 会炸。
            # 统一降级成 raw，和"非 JSON"走同一条路。
            _log(f"[mqtt] {topic} 不是 JSON 对象（{type(payload).__name__}），按原文处理")
            payload = {"raw": msg.payload.decode("utf-8", "ignore")}
        if topic == CMD_ACK_TOPIC:
            bot.on_command_ack(payload)
            return
        content = format_alert(topic, payload)
        _log(f"[mqtt] {topic} -> 推送")
        bot.send_markdown(content)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pihub-wecom-bot")
    client.on_connect = on_connect
    # 兜底边界：漏网的异常不许冲出回调，否则 loop_start 的网络线程会静默退出
    # （进程还活着、WS 还连着，但再也收不到消息）——见 mqtt_guard 的说明。
    client.on_message = mqtt_guard.guard(on_message, _log)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    bot.mqtt = client
    return client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", metavar="TEXT", help="直接推送一条测试消息到已记录会话")
    ap.add_argument("--chatid", help="指定测试推送的 chatid")
    args = ap.parse_args()

    if args.send_test:
        bot = SmallTvBot()
        # 测试模式：临时建立连接→订阅→推送→退出
        bot.ws = websocket.WebSocketApp(
            WS_URL, on_open=bot._on_open, on_message=bot._on_message,
            on_error=bot._on_error, on_close=bot._on_close)
        t = threading.Thread(target=bot.ws.run_forever, kwargs={"ping_interval": 0}, daemon=True)
        t.start()
        if not bot.subscribed.wait(timeout=10):
            _log("[test] 订阅超时")
        time.sleep(1)
        bot.send_markdown(args.send_test, chatid=args.chatid)
        time.sleep(2)
        bot.stop_bot()
        return

    bot = SmallTvBot()
    mqtt_client = start_mqtt(bot, None)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        _log("[main] 退出")
    finally:
        mqtt_client.loop_stop()


if __name__ == "__main__":
    main()
