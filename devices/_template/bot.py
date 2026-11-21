#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""<name> 适配器 —— 从 devices/_template/ 复制而来。

★ 这是「加一台设备」的最小骨架：只有 MQTT 这一半（订阅命令 → 执行 → 回执）。
  设备还需要企微机器人时，再照 devices/smalltv/bot.py 那样继承 core/wecom_base.py。

★ 复制后要填的六处（全部标着 TODO）：
  1) DEVICE          设备名（决定主题、单元名、ACL 用户）
  2) CMD_TOPIC       命令主题
  3) ACK_TOPIC       回执主题
  4) parse_command   文本 → (cmd, arg)
  5) describe_ack    回执 payload → 一句人话
  6) execute         真正驱动设备的那一步（HTTP / 串口 / 别的总线）

运行：<部署目录>/.venv/bin/python devices/<name>/bot.py
"""
import json
import os
import signal
import sys
import threading
from datetime import datetime

# —— 把仓库根放进 sys.path，core.* 才 import 得到 ——
_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "core")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        raise SystemExit(f"找不到仓库根：{__file__} 之上没有 core/")
    _p = _parent
if _p not in sys.path:
    sys.path.insert(0, _p)

import paho.mqtt.client as mqtt
from core.config import get as cfg
from core import mqtt_guard

# ---------- TODO 1-3：设备名与主题 ----------
DEVICE = "<name>"
CMD_TOPIC = f"hub/command/{DEVICE}"
ACK_TOPIC = f"hub/command/{DEVICE}/ack"

# QoS 1 会重投，同一个 id 只执行一次（物理动作不可回滚，重发 = 多做一次）
_seen = set()


def _log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ---------- TODO 4：文本 → (cmd, arg) ----------
def parse_command(text):
    """认不出就返回 (None, None)。模板只认 ping，照它写你自己的。"""
    t = (text or "").strip().lower()
    if t == "ping":
        return "ping", None
    return None, None


# ---------- TODO 5：回执 → 一句人话 ----------
def describe_ack(payload):
    return f"{DEVICE}: {payload.get('status', '?')}"


# ---------- TODO 6：真正驱动设备 ----------
def execute(cmd, arg):
    """返回 {"status": ...}。**唯一该出现设备协议的地方。**

    ⚠️ 这里是唯一该出现设备协议的地方 —— 别的模块不该知道设备怎么说话。
    """
    if cmd == "ping":
        return {"status": "pong"}
    return {"status": "unsupported", "cmd": cmd}


def main():
    host = cfg("mqtt_host") or "127.0.0.1"
    port = int(float(cfg("mqtt_port") or 1883))
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"pihub-{DEVICE}")

    def on_connect(c, userdata, flags, rc, props=None):
        c.subscribe(CMD_TOPIC, qos=1)
        _log(f"[mqtt] connected {host}:{port}, subscribed {CMD_TOPIC}")

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            _log(f"[cmd] 非 JSON，丢弃: {msg.payload[:80]!r}")
            return
        if not isinstance(payload, dict):
            _log("[cmd] 不是 JSON 对象，丢弃")
            return
        rid = payload.get("id") or payload.get("req_id")
        if rid and rid in _seen:
            _log(f"[cmd] 重复投递，忽略 id={rid}")
            return
        if rid:
            _seen.add(rid)
        cmd = payload.get("cmd") or payload.get("action")
        arg = payload.get("arg") or payload.get("value")
        result = execute(cmd, arg)
        ack = {"v": 1, "id": rid, "ok": result.get("status") != "unsupported"}
        ack.update(result)
        c.publish(ACK_TOPIC, json.dumps(ack, ensure_ascii=False), qos=1)
        _log(f"[ack] {describe_ack(ack)}")

    client.on_connect = on_connect
    # 兜底边界：漏网的异常不许冲出回调，否则 loop_start 的网络线程会静默退出
    client.on_message = mqtt_guard.guard(on_message, _log)
    client.connect(host, port, keepalive=30)

    # 用 loop_start + 事件等待，而不是 loop_forever：
    # loop_forever 不响应 SIGTERM，systemctl stop 必然等到超时。
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    client.loop_start()
    stop.wait()
    client.loop_stop()
    client.disconnect()
    _log("[main] 退出")


if __name__ == "__main__":
    main()
