#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 设备机器人（企微智能机器人，长连接 API 模式）。

与 C3 小电视机器人（wecom_bot.py）的关系：
  - **完全独立进程、独立 bot 账号、独立 MQTT 主题**
  - 复用 wecom_bot.py 的 WeComBot 类（WS 长连接 / 心跳 / 重连退避 / 被动回复），
    只覆盖「指令解析 + 主题 + 回显」三处；wecom_bot.py 本身一行不改。
  - 为什么复用而不是复制全文件：心跳线程泄漏、退避不重置、pending 无界增长那些坑
    已经修过一轮了（见 wecom_bot.py 里的注释），复制一份等于让它们再犯一次。

链路：
  企微消息 → p4_command.parse_command → hub/command/p4
                                      → P4 执行 → hub/command/p4/ack
                                      → 按 id 配对 → response_url 回显

超时：5 秒（用户 2026-10-28 定，P4 打开 app 基本秒开）。超时回「P4 暂无响应」，
措辞与 C3 保持一致，不吓人也不假装成功。

运行：/home/pi/cooperate/pihub/.venv/bin/python app/p4_bot.py
"""
import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websocket  # noqa: E402
import paho.mqtt.client as mqtt  # noqa: E402

from wecom_bot import (  # noqa: E402  复用成熟实现，见文件头说明
    WeComBot, WS_URL, _load_env, _log, cfg,
)
import mqtt_guard  # noqa: E402
import p4_command  # noqa: E402

# ---------------- P4 专用常量 ----------------
CMD_TOPIC = "hub/command/p4"
CMD_ACK_TOPIC = "hub/command/p4/ack"

ACK_TIMEOUT_SEC = 5.0       # 用户定：5 秒无回执 ⇒ P4 暂无响应
ACK_SWEEP_SEC = 1.0         # 扫描间隔（比 C3 的 15s 细得多，因为 TTL 只有 5s）


class P4Bot(WeComBot):
    """只改四处：凭据来源、指令路由、ack 配对超时、**推送目标配置键**。"""

    # ★ 必须覆盖：三个 bot 共用一个库，共用 wecom_chat_id 会互相改写推送目标
    TARGET_KEY = "wecom_chat_id_p4"
    TYPE_KEY = "wecom_chat_type_p4"
    CHAT_LABEL = "P4 设备"

    def __init__(self):
        # 先把 P4 凭据塞进环境变量，让父类 __init__ 原样读取。
        # 这样父类逻辑（含 url/心跳）完全不用改。
        env = _load_env()
        bid = os.environ.get("P4_WECOM_BOT_ID") or env.get("P4_WECOM_BOT_ID", "")
        sec = os.environ.get("P4_WECOM_BOT_SECRET") or env.get("P4_WECOM_BOT_SECRET", "")
        if not bid or not sec:
            raise SystemExit("缺少 P4_WECOM_BOT_ID / P4_WECOM_BOT_SECRET（应在 pihub/.env）")
        os.environ["WECOM_BOT_ID"] = bid
        os.environ["WECOM_BOT_SECRET"] = sec
        super().__init__()
        # 换成 P4 自己的 pending 表（内容同构：req_id -> (response_url, ts, cmd)）
        self.pending = {}
        self._pending_lock = threading.Lock()

    # ---------- 指令下发 ----------
    def send_p4_command(self, cmd, arg, response_url=None, raw_text=""):
        """发指令到 hub/command/p4，并登记等待 ack。"""
        req_id = uuid.uuid4().hex
        payload = {"v": 1, "id": req_id, "cmd": cmd}
        if arg:
            payload["arg"] = arg

        if response_url:
            with self._pending_lock:
                # 存 cmd 而不是 tid：回显时要用它判断「关的是音乐还是小智」，
                # 虽然 ack 里也带回 cmd，但超时路径没有 ack，只能用本地这份。
                self.pending[req_id] = (response_url, time.time(), cmd)

        if self.mqtt is None:
            _log("[cmd] MQTT 未连接，无法发 P4 指令")
            if response_url:
                self.reply(response_url, "指令总线未连接，请稍后重试")
            return False
        self.mqtt.publish(CMD_TOPIC, json.dumps(payload, ensure_ascii=False), qos=1)
        _log(f"[cmd] → {CMD_TOPIC} {json.dumps(payload, ensure_ascii=False)}  (原文={raw_text[:40]!r})")
        return True

    # ---------- 回执处理 ----------
    def on_p4_ack(self, payload):
        """收到 hub/command/p4/ack：按 id 找回 response_url 并回显。"""
        req_id = payload.get("id")  # P4 原样回传 id
        text = p4_command.describe_ack(payload)
        with self._pending_lock:
            ent = self.pending.pop(req_id, None)
        rurl = ent[0] if ent else None
        if rurl:
            self.reply(rurl, text)
        else:
            # 无待回复（例如超时后 ack 才到，或别的来源发的指令）→ 主动推默认会话
            _log(f"[ack] 无匹配 pending（id={req_id}），改推默认会话")
            self.send_markdown(text)
        _log(f"[ack] id={req_id} {text}")

    # ---------- 超时扫描（5s，比 C3 细） ----------
    def _sweep_pending(self):
        now = time.time()
        with self._pending_lock:
            stale = [(k, v) for k, v in self.pending.items()
                     if now - v[1] >= ACK_TIMEOUT_SEC]
            for k, _v in stale:
                self.pending.pop(k, None)
            # 兜底防无界增长
            if len(self.pending) > 100:
                for k, _v in sorted(self.pending.items(), key=lambda kv: kv[1][1])[:-100]:
                    self.pending.pop(k, None)
        for _k, (rurl, ts, cmd) in stale:
            text = "P4 暂无响应"
            _log(f"[timeout] cmd={cmd} 等待 {int(now - ts)}s 无 ack")
            if not self.reply(rurl, text):
                self.send_markdown(text)

    def _pending_sweeper_loop(self):
        while not self.stop:
            time.sleep(ACK_SWEEP_SEC)
            if self.stop:
                return
            try:
                self._sweep_pending()
            except Exception as e:
                _log(f"[pending] 扫描异常: {e}")

    # ---------- 消息路由（覆盖父类） ----------
    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            _log(f"[ws] 非JSON: {raw[:200]}")
            return
        cmd = msg.get("cmd", "")

        if cmd == "aibot_subscribe" or (not cmd and "errcode" in msg and not self.subscribed.is_set()):
            rc = msg.get("errcode", msg.get("result", {}).get("errcode"))
            if rc == 0:
                self.subscribed.set()
                _log("[ws] subscribe ok (P4 bot)")
            else:
                _log(f"[ws] subscribe 失败: {msg}")
            return

        if cmd == "aibot_msg_callback":
            body = msg.get("body", {})
            ctype = 1 if body.get("chattype") == "single" else 2
            cid = body.get("chatid") or (body.get("from", {}) or {}).get("userid")
            if cid:
                self.save_chat(cid, ctype)
            text = (body.get("text") or {}).get("content", "")
            _log(f"[ws] 收到消息: {text[:80]!r}")
            rurl = body.get("response_url")
            parsed_cmd, arg = p4_command.parse_command(text)
            if parsed_cmd == "help":
                self.reply(rurl, p4_command.HELP_TEXT)
            elif parsed_cmd:
                self.send_p4_command(parsed_cmd, arg, response_url=rurl, raw_text=text)
            else:
                # 未识别：回引导（同时证明消息到了）
                self.reply(rurl, "未识别该指令。\n" + p4_command.HELP_TEXT)
            return

        if cmd == "aibot_event_callback":
            ev = (msg.get("body", {}).get("event") or {}).get("eventtype", "")
            if ev == "disconnected_event":
                _log("[ws] 服务端断开旧连接事件")
                self.subscribed.clear()
                try:
                    ws.close()
                except Exception:
                    pass
            return

        if cmd in ("aibot_respond_msg", "aibot_send_msg", "aibot_respond_welcome_msg",
                   "aibot_respond_update_msg", "ping"):
            rc = msg.get("errcode")
            if rc not in (0, None):
                _log(f"[ws] {cmd} resp: {msg}")
            return

        if not cmd:
            return
        _log(f"[ws] 未处理 cmd={cmd!r} raw={raw[:300]}")

    # ---------- run_forever：起 P4 版扫描线程 ----------
    def run_forever(self):
        threading.Thread(target=self._heartbeat_loop, daemon=True,
                         name="p4-heartbeat").start()
        threading.Thread(target=self._pending_sweeper_loop, daemon=True,
                         name="p4-pending-sweeper").start()
        self._base_run_forever()

    def _base_run_forever(self):
        """复用父类的重连循环，但不再起它的心跳/扫描线程。"""
        while not self.stop:
            try:
                _log(f"[ws] 连接 {WS_URL} ...")
                self.ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=0)
            except Exception as e:
                _log(f"[ws] run_forever 异常: {e}")
            if self.stop:
                break
            _log(f"[ws] {self._backoff}s 后重连")
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 30)


# ---------------- MQTT ----------------
def start_mqtt(bot):
    host = cfg("mqtt_host") or "127.0.0.1"
    port = int(float(cfg("mqtt_port") or 1883))

    def on_connect(client, userdata, flags, rc, props=None):
        client.subscribe(CMD_ACK_TOPIC, qos=1)
        _log(f"[mqtt] connected {host}:{port}, subscribed {CMD_ACK_TOPIC}")

    def on_message(client, userdata, msg):
        if msg.topic != CMD_ACK_TOPIC:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            _log(f"[mqtt] ack 非JSON: {msg.payload[:200]}")
            return
        if not isinstance(payload, dict):
            # 合法 JSON 但不是对象 ⇒ on_p4_ack 里的 .get() 会抛 AttributeError，
            # 而 paho 不捕获回调异常 ⇒ loop_start 的网络线程静默退出。
            _log(f"[mqtt] ack 不是 JSON 对象（{type(payload).__name__}），丢弃: "
                 f"{msg.payload[:120]!r}")
            return
        bot.on_p4_ack(payload)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pihub-p4-bot")
    client.on_connect = on_connect
    # 兜底边界：见 mqtt_guard 的说明（网络线程静默退出的那种坏法最难发现）
    client.on_message = mqtt_guard.guard(on_message, _log)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    bot.mqtt = client
    return client


def main():
    bot = P4Bot()
    mqtt_client = start_mqtt(bot)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        _log("[main] 退出")
    finally:
        mqtt_client.loop_stop()


if __name__ == "__main__":
    main()
