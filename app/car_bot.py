#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小车（曼波语音小车）企微机器人 —— 长连接 API 模式。

与 P4 机器人（p4_bot.py）的关系：
  - 完全独立进程、独立 bot 账号、独立 MQTT 主题
  - 复用 wecom_bot.py 的 WeComBot（WS 长连接 / 心跳 / 重连退避 / 被动回复），
    只覆盖「凭据来源 + 指令路由 + 主题 + 回执配对」几处；wecom_bot.py 一行不改。

链路：
  企微消息 → car_command.parse_command → hub/command/car
            → 语音核心控制.py 执行 → hub/command/car/ack
            → 按 id 配对 → response_url 回显

★ 与 P4 的关键差异（2026-10-30 用户定）：
  小车动作很长（露一手 ≈ 转3圈 + 21s 音乐），所以**收到即回「已受理」**，
  不再等动作跑完（不像 P4 那样 5 秒超时）。动作结束后车侧会补发一条 done 回执，
  那条走「无 pending → 主动推默认会话」路径，相当于异步结果通知。

运行：/home/pi/cooperate/pihub/.venv/bin/python app/car_bot.py
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

from wecom_bot import (  # noqa: E402
    WeComBot, WS_URL, _load_env, _log, cfg,
)
import mqtt_guard  # noqa: E402
import car_command  # noqa: E402

# ---------------- 小车专用常量 ----------------
CMD_TOPIC = "hub/command/car"
CMD_ACK_TOPIC = "hub/command/car/ack"

# ★ 只等「已受理」，最多 5 秒（本地 MQTT 正常是毫秒级）
ACK_TIMEOUT_SEC = 5.0
ACK_SWEEP_SEC = 1.0

# 指令 → 受理措辞（立刻回给企微，不用等动作跑完）
ACCEPT_TEXT = {
    "stop": "收到，已发送停止指令",
    "showoff": "露一手：开始转 3 圈 + 播放 EG 音乐（约 30 秒）",
    "spin": "开始转一圈",
    "music": "开始播放歌库",
}


class CarBot(WeComBot):
    """只改五处：凭据来源、指令路由、ack 配对、受理措辞、**推送目标配置键**。"""

    # ★ 必须覆盖：三个 bot 共用一个库，共用 wecom_chat_id 会互相改写推送目标
    TARGET_KEY = "wecom_chat_id_car"
    TYPE_KEY = "wecom_chat_type_car"
    CHAT_LABEL = "小车"

    def __init__(self):
        env = _load_env()
        bid = os.environ.get("CAR_WECOM_BOT_ID") or env.get("CAR_WECOM_BOT_ID", "")
        sec = os.environ.get("CAR_WECOM_BOT_SECRET") or env.get("CAR_WECOM_BOT_SECRET", "")
        if not bid or not sec:
            raise SystemExit("缺少 CAR_WECOM_BOT_ID / CAR_WECOM_BOT_SECRET（应在 pihub/.env）")
        os.environ["WECOM_BOT_ID"] = bid
        os.environ["WECOM_BOT_SECRET"] = sec
        super().__init__()
        self.pending = {}
        self._pending_lock = threading.Lock()

    # ---------- 指令下发 ----------
    def send_car_command(self, cmd, response_url=None, raw_text=""):
        req_id = uuid.uuid4().hex
        payload = {"v": 1, "id": req_id, "cmd": cmd}

        if response_url:
            with self._pending_lock:
                self.pending[req_id] = (response_url, time.time(), cmd)

        if self.mqtt is None:
            _log("[cmd] MQTT 未连接，无法发小车指令")
            if response_url:
                self.reply(response_url, "指令总线未连接，请稍后重试")
            return False
        self.mqtt.publish(CMD_TOPIC, json.dumps(payload, ensure_ascii=False), qos=1)
        _log(f"[cmd] → {CMD_TOPIC} {json.dumps(payload, ensure_ascii=False)}  (原文={raw_text[:40]!r})")
        return True

    # ---------- 回执处理 ----------
    def on_car_ack(self, payload):
        req_id = payload.get("id")
        status = payload.get("status", "")
        with self._pending_lock:
            ent = self.pending.pop(req_id, None)

        # ★ done/error 是动作结束后的异步通知，不是对我们 pending 的回应
        if status in ("done", "error") and ent is None:
            text = car_command.describe_ack(payload)
            _log(f"[ack] 异步完成通知 id={req_id} {text}")
            self.send_markdown(text)
            return

        text = car_command.describe_ack(payload)
        rurl = ent[0] if ent else None
        if rurl:
            self.reply(rurl, text)
        else:
            _log(f"[ack] 无匹配 pending（id={req_id}），改推默认会话")
            self.send_markdown(text)
        _log(f"[ack] id={req_id} {text}")

    # ---------- 超时扫描 ----------
    def _sweep_pending(self):
        now = time.time()
        with self._pending_lock:
            stale = [(k, v) for k, v in self.pending.items()
                     if now - v[1] >= ACK_TIMEOUT_SEC]
            for k, _v in stale:
                self.pending.pop(k, None)
            if len(self.pending) > 100:
                for k, _v in sorted(self.pending.items(), key=lambda kv: kv[1][1])[:-100]:
                    self.pending.pop(k, None)
        for _k, (rurl, ts, cmd) in stale:
            text = "小车暂无响应（%s）" % car_command.CMD_NAMES.get(cmd, cmd)
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

    # ---------- 消息路由 ----------
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
                _log("[ws] subscribe ok (car bot)")
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
            parsed = car_command.parse_command(text)
            if parsed == "help":
                self.reply(rurl, car_command.HELP_TEXT)
            elif parsed:
                ok = self.send_car_command(parsed, response_url=rurl, raw_text=text)
                if ok and parsed in ACCEPT_TEXT:
                    # ★ 立刻回「已受理」，不等动作跑完
                    with self._pending_lock:
                        # 受理消息已发出，pending 里的条目继续等 done/超时
                        pass
                    self.reply(rurl, ACCEPT_TEXT[parsed])
                    with self._pending_lock:
                        # 已受理 ⇒ 从 pending 移出，后续 done 走主动推会话
                        for k, v in list(self.pending.items()):
                            if v[0] == rurl:
                                self.pending.pop(k, None)
                                break
            else:
                self.reply(rurl, "未识别该指令。\n" + car_command.HELP_TEXT)
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

    # ---------- run_forever ----------
    def run_forever(self):
        threading.Thread(target=self._heartbeat_loop, daemon=True,
                         name="car-heartbeat").start()
        threading.Thread(target=self._pending_sweeper_loop, daemon=True,
                         name="car-pending-sweeper").start()
        self._base_run_forever()

    def _base_run_forever(self):
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
            # 合法 JSON 但不是对象 ⇒ on_car_ack 里的 .get() 会抛 AttributeError，
            # 而 paho 不捕获回调异常 ⇒ loop_start 的网络线程静默退出。
            _log(f"[mqtt] ack 不是 JSON 对象（{type(payload).__name__}），丢弃: "
                 f"{msg.payload[:120]!r}")
            return
        bot.on_car_ack(payload)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pihub-car-bot")
    client.on_connect = on_connect
    # 兜底边界：见 mqtt_guard 的说明（网络线程静默退出的那种坏法最难发现）
    client.on_message = mqtt_guard.guard(on_message, _log)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    bot.mqtt = client
    return client


def main():
    bot = CarBot()
    mqtt_client = start_mqtt(bot)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        _log("[main] 退出")
    finally:
        mqtt_client.loop_stop()


if __name__ == "__main__":
    main()
