#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微智能机器人 —— 长连接基类（**不含任何设备**）。

三个设备机器人（`devices/smalltv/bot.py`、`devices/p4/bot.py`、`devices/car/bot.py`）
都继承这里的 `WeComBot`。本文件只放它们**真正共用**的那部分：

  - WebSocket 长连接 / 订阅 / 心跳 / 重连退避（连上即归零）
  - 被动回复（response_url）与主动推送（aibot_send_msg）
  - chatid 持久化（按 bot 分键，互不覆盖）
  - **回执配对**：pending 表 + 超时扫描 + 无 pending 时改推默认会话

设备自己的东西一律不在这里：主题、指令表、回执渲染、凭据键、日志落点。
接一台新设备的契约见 docs/DEVICE-PLUGIN.md。

关键约束（官方文档 §7）：
  - 「主动推送」要求用户先给机器人发过消息，才可向该会话推送
    → 首次收到 aibot_msg_callback 时把 chatid 落 SQLite，后续复用。
  - 心跳：每 30s 发 ping 保活；断线需自动重连。
  - 频率限制：单会话 30 条/分钟，1000 条/小时（告警侧另有冷却）。
"""
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime

# —— B2：把仓库根放进 sys.path，core.* / devices.* 才 import 得到 ——
# 往上退到「含 core/ 的那一层」为止，不数层数（数层数正是证书路径踩过的坑）。
_p = os.path.dirname(os.path.abspath(__file__))
while not os.path.isdir(os.path.join(_p, "core")):
    _parent = os.path.dirname(_p)
    if _parent == _p:
        raise SystemExit(f"找不到仓库根：{__file__} 之上没有 core/")
    _p = _parent
if _p not in sys.path:
    sys.path.insert(0, _p)
import websocket  # websocket-client
from core.config import get as cfg

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
DB_PATH = os.environ.get("PIHUB_DB", os.path.join(PROJECT, "data", "pihub.db"))


def _load_env():
    """读 .env（不进 git 的密钥）。"""
    out = {}
    path = os.path.join(PROJECT, ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out

WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_SEC = 30

# 重连退避：起点 3s，每次失败翻倍，封顶 30s。
# 封顶是有意的（服务端打不通时别疯狂重连）；但**连上之后必须回到起点** ——
# 原先只增不减，导致一次抖动之后每次断线都按封顶等，详见 _on_open 的说明。
BACKOFF_START = 3
BACKOFF_MAX = 30

# 「等待回执」记录的超时与上限（详见 _sweep_pending 的说明）。
# 45s 的依据：正常回执路径 = 适配器内部处理 + 设备往返 + 回程，
# 实测在 15s 内完成；等满 45s 基本可以断定这条回执不会来了。
_PENDING_TTL = 45          # 秒：超过仍未收到 ack ⇒ 判「结果未知」
_PENDING_MAX = 200         # 硬上限兜底，防无界增长
_PENDING_SWEEP_SEC = 15    # 扫描间隔

def _log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ---------------- chatid 持久化 ----------------
def save_chat(chatid, chat_type, label=None,
              target_key="wecom_chat_id_default", type_key="wecom_chat_type_default"):
    """记录可主动推送的会话（首次收到用户消息时）。

    ★ 为什么带 target_key（2026-10-31）：
    三个设备机器人是三个进程，但**共用同一个 SQLite 库**，而 config 表里的
    那一个键是**全局唯一键** —— 谁最后收到消息，谁就顺手改掉了
    告警机器人的推送目标。后果不是报错，而是**静默发错群**：
    A 设备的 bot 把告警发到 B 设备那个会话时，它根本不在那个群、企微会拒收；
    而 send_markdown 的 ok 只看 websocket 有没有发出去，不看服务端 errcode。
    ⇒ 每个 bot 用自己的一组键（TARGET_KEY / TYPE_KEY），互不覆盖。

    📌 本仓作者自己的部署里三个机器人恰好同群（chatid 相同），所以看不出来；
    但 DEPLOY.md 写着"各建一个机器人"，别人很可能分成三个群 ⇒ 必踩。
    """
    now = int(time.time())
    with _conn() as c:
        c.execute("""
            INSERT INTO wecom_chat(chatid, chat_type, label, active, last_seen)
            VALUES (?,?,?,1,?)
            ON CONFLICT(chatid) DO UPDATE SET
              chat_type=excluded.chat_type, last_seen=excluded.last_seen
        """, (chatid, chat_type, label, now))
        # 同时写 config（按 bot 分键），便于其它组件读取该 bot 的默认目标
        for key, val in ((target_key, chatid), (type_key, str(chat_type))):
            c.execute("""INSERT INTO config(key,value,updated_at) VALUES(?,?,?)
                         ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value, updated_at=excluded.updated_at""",
                      (key, val, now))


def get_target_chat(target_key="wecom_chat_id_default", type_key="wecom_chat_type_default",
                    label=None):
    """返回 (chatid, chat_type)；优先**本 bot 自己的键**，其次最近活跃会话。"""
    cid = cfg(target_key)
    ctype = int(float(cfg(type_key) or 2))
    if cid:
        return cid, ctype
    with _conn() as c:
        # 冷启动兜底（这个 bot 还没收到过任何消息）：优先同 label 的会话，
        # 但**不硬性只认 label** —— 单聊的 label 是 NULL，硬过滤会让它永远找不到。
        row = c.execute(
            "SELECT chatid,chat_type FROM wecom_chat WHERE active=1 "
            "ORDER BY (label IS ?) DESC, last_seen DESC LIMIT 1",
            (label,)).fetchone()
    if row:
        return row["chatid"], row["chat_type"]
    return None, ctype


# ---------------- WebSocket 长连接 ----------------
class WeComBot:
    # ★ 每个设备一套「主动推送目标」的配置键与会话标签（详见 save_chat 的说明）。
    #   **设备插件必须覆盖这两个**——否则多个 bot 会互相改写对方的推送目标。
    #   这里的默认值只给"忘了覆盖"兜底，故意取得让它们彼此不撞。
    TARGET_KEY = "wecom_chat_id_default"
    TYPE_KEY = "wecom_chat_type_default"
    CHAT_LABEL = "告警群"

    def __init__(self, on_message=None):
        # 凭证统一从 .env / 环境变量读取（§1：密钥不进 git，不入 config 表）
        env = _load_env()
        self.bot_id = os.environ.get("WECOM_BOT_ID") or env.get("WECOM_BOT_ID", "")
        self.secret = os.environ.get("WECOM_BOT_SECRET") or env.get("WECOM_BOT_SECRET", "")
        self.ws = None
        self.subscribed = threading.Event()
        self.stop = False
        self.on_message = on_message  # 收到消息时的可选回调(body)
        self._last_hb = 0
        self._send_lock = threading.Lock()
        self.mqtt = None  # 由 start_mqtt 注入（发指令用）
        self.pending = {}  # req_id -> (response_url, 发起时刻, tid)；等 devctl 回执后回群，超时见 _sweep_pending
        self._pending_lock = threading.Lock()
        self._backoff = BACKOFF_START  # 重连退避当前值；连上即重置，见 _on_open

    # ---------- chatid：本 bot 自己的目标 ----------
    def save_chat(self, chatid, chat_type, label=None):
        """记下**本 bot**的可推送会话（用子类覆盖的 TARGET_KEY / TYPE_KEY）。"""
        save_chat(chatid, chat_type, label, self.TARGET_KEY, self.TYPE_KEY)

    def get_target_chat(self):
        """取**本 bot**的推送目标；没记录过才回退到最近的活跃会话。"""
        return get_target_chat(self.TARGET_KEY, self.TYPE_KEY, self.CHAT_LABEL)

    def _now(self):
        return int(time.time())

    def _new_req_id(self):
        return uuid.uuid4().hex

    def _send(self, obj):
        with self._send_lock:
            if self.ws is None:
                return False
            try:
                self.ws.send(json.dumps(obj, ensure_ascii=False))
                return True
            except Exception as e:
                _log(f"[ws] send 失败: {e}")
                return False

    def subscribe(self):
        self._send({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self._new_req_id()},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        })

    def send_markdown(self, content, chatid=None, chat_type=None):
        """主动推送 markdown 到指定会话（aibot_send_msg）。"""
        if chatid is None:
            chatid, chat_type = self.get_target_chat()
        if not chatid:
            _log("[send] 无可用 chatid（需用户先给机器人发消息），跳过推送")
            return False
        if chat_type is None:
            chat_type = 2
        ok = self._send({
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._new_req_id()},
            "body": {"chatid": chatid, "chat_type": int(chat_type),
                     "msgtype": "markdown", "markdown": {"content": content}},
        })
        _log(f"[send] markdown -> {chatid}(type={chat_type}) ok={ok}")
        return ok

    def reply(self, response_url, content):
        """用 response_url 被动回复（§101138）。无需 chatid，直接 HTTP POST。"""
        if not response_url:
            return False
        try:
            import requests as _rq
            r = _rq.post(response_url, json={
                "msgtype": "markdown", "markdown": {"content": content}},
                timeout=6)
            _log(f"[reply] -> {r.status_code} {r.text[:120]}")
            return r.status_code == 200
        except Exception as e:
            _log(f"[reply] 失败: {e}")
            return False

    def on_command_ack(self, payload):
        """收到本设备的 ack 主题，找回 response_url 回群。"""
        req_id = payload.get("req_id")
        ok = payload.get("ok")
        msg = payload.get("msg", "")
        text = ("✅ " if ok else "⚠️ ") + msg
        with self._pending_lock:
            ent = self.pending.pop(req_id, None)
        rurl = ent[0] if ent else None
        if rurl:
            self.reply(rurl, text)
        else:
            # 无待回复 url 时，主动推到默认会话
            self.send_markdown(text)
        _log(f"[ack] {text}")

    # ---------------- 等待回执的超时清理 ----------------
    def _sweep_pending(self):
        """清掉超时未回执的记录，并**明确回一句「结果未知」**。

        为什么需要：send_command 把 response_url 记进 self.pending，等 devctl 的 ack 回来再取用。
        但 ack 可能**永远不来**（执行侧没起 / MQTT 断 / 指令根本没落到执行侧）。旧版只有
        「收到 ack 才删」，于是：① 执行器离线期间每条指令都留一条记录，反复重试则内存持续增长；
        ② 发起方**永远等不到任何回音**，只能一直干等或盲目重试。

        措辞用**「结果未知」而不是「失败」** —— 超时既可能是「没执行」，也可能是
        「执行了但回执丢了」，这两者从本侧分不出来，说成失败会误导发起方去重试。
        """
        now = time.time()
        with self._pending_lock:
            stale = [(k, v) for k, v in self.pending.items() if now - v[1] >= _PENDING_TTL]
            for k, _v in stale:
                self.pending.pop(k, None)
            dropped = 0
            if len(self.pending) > _PENDING_MAX:
                # 兜底：即便 TTL 未到也不能让它无界增长。正常不该走到这条路径；
                # 被丢掉的记录若随后有 ack 回来，会走 on_command_ack 的「主动推默认会话」分支。
                for k, _v in sorted(self.pending.items(), key=lambda kv: kv[1][1])[:-_PENDING_MAX]:
                    self.pending.pop(k, None)
                    dropped += 1
        if dropped:
            _log(f"[pending] 超过上限 {_PENDING_MAX}，丢弃最旧的 {dropped} 条")

        for _k, (rurl, ts, tid) in stale:
            waited = int(now - ts)
            tag = f"[{tid}] " if tid else ""
            msg = (f"{tag}未收到执行回执（已等待 {waited} 秒），**本次结果未知** —— "
                   f"既可能已执行、也可能没有。请先确认设备状态，不要盲目重发。")
            if self.reply(rurl, msg):
                _log(f"[pending] 已回报「结果未知」（等待 {waited}s, tid={tid}）")
            else:
                # response_url 通常有有效期，过期后只能改推默认会话，否则这条信号就丢了
                _log(f"[pending] response_url 回报失败，改推默认会话（tid={tid}）")
                self.send_markdown("⚠️ " + msg)

    def _pending_sweeper(self):
        """常驻扫描线程。与心跳同理：整个进程只起这一个，且必须在重连循环【外】起。

        单独开线程而不挂在心跳循环上：回报要发 HTTP（reply 超时 6s，失败还会再推一次），
        而心跳的职责是每 30s 发 ping 保活 —— 被拖慢有掉线的风险。
        """
        while not self.stop:
            time.sleep(_PENDING_SWEEP_SEC)
            if self.stop:
                return
            try:
                self._sweep_pending()
            except Exception as e:
                _log(f"[pending] 清理异常: {e}")

    def _on_open(self, ws):
        _log(f"[ws] connected, subscribe bot_id={self.bot_id}")
        # 连上了 ⇒ 网络与服务端都是通的，退避归零。
        #
        # 修的是这个缺陷：退避原先在 run_forever 里【只增不减】，从不重置。
        # 于是只要经历过几次抖动，此后【每次】断线都要按封顶等 30s ——
        # 哪怕这次断线与上次毫无关系、网络也早就好了。
        # 而断线窗口内企微**不下发也不补发** @ 本机器人的消息 ⇒ 窗口越长丢得越多。
        #
        # 为什么放这里、而不是"run_forever 返回之后"：run_forever 在
        # 【连上后被关闭】与【压根没连上】两种情况下都会正常返回，看返回值分不清。
        # _on_open 是"确实连上过"的唯一可靠信号。
        #
        # 权衡：若服务端出现"接受连接后立刻断开"的反复，这里会以 3s 的节奏重试，
        # 而不是逐步退避到 30s。3s ≈ 每分钟 20 次，不构成重试风暴；
        # 而"要求连接稳定 N 秒才重置"要多引入一个可调参数，只换来这个未观察到的场景，故不采用。
        if self._backoff != BACKOFF_START:
            _log(f"[ws] 连接成功，退避重置 {self._backoff}s -> {BACKOFF_START}s")
        self._backoff = BACKOFF_START
        self.subscribe()

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            _log(f"[ws] 非JSON: {raw[:200]}")
            return
        cmd = msg.get("cmd", "")
        if cmd == "aibot_subscribe" or (not cmd and "errcode" in msg and not self.subscribed.is_set()):
            # 订阅响应：无 cmd，靠 req_id 回执；errcode=0 即成功
            rc = msg.get("errcode", msg.get("result", {}).get("errcode"))
            if rc == 0:
                self.subscribed.set()
                _log("[ws] subscribe ok")
            else:
                _log(f"[ws] subscribe 失败: {msg}")
            return
        if cmd == "aibot_msg_callback":
            body = msg.get("body", {})
            ctype = 1 if body.get("chattype") == "single" else 2
            cid = body.get("chatid") or (body.get("from", {}) or {}).get("userid")
            if cid:
                self.save_chat(cid, ctype,
                               label=self.CHAT_LABEL if ctype == 2 else None)
                _log(f"[ws] 记录会话 chatid={cid} type={ctype}")
            text = (body.get("text") or {}).get("content", "")
            _log(f"[ws] 收到消息: {text[:80]}")
            # 指令的处理属于设备插件（核心不认识任何设备的指令集）
            self.on_text(body, text)
            return
        if cmd == "aibot_event_callback":
            body = msg.get("body", {})
            ev = (body.get("event") or {}).get("eventtype", "")
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
        # 无 cmd 的响应（心跳/推送回执）静默处理，仅在错误时记录
        if not cmd:
            rc = msg.get("errcode")
            if rc not in (0, None):
                _log(f"[ws] 响应错误: {msg}")
            return
        _log(f"[ws] 未处理 cmd={cmd!r} raw={raw[:300]}")

    # ---------- 收到用户文本：设备插件覆盖这里 ----------
    def on_text(self, body, text):
        """基类默认：不认任何指令，只把 body 交给 on_message 回调（若有）。

        设备插件覆盖本方法来实现自己的指令表 —— 核心不认识任何设备的指令集。
        """
        if self.on_message:
            try:
                self.on_message(body)
            except Exception as e:
                _log(f"[ws] on_message 异常: {e}")

    def _on_error(self, ws, err):
        _log(f"[ws] error: {err}")

    def _on_close(self, ws, code, msg):
        self.subscribed.clear()
        _log(f"[ws] closed code={code} msg={msg}")

    def run_forever(self):
        # 心跳线程【整个进程只起这一个】。
        # 旧版把它放在重连循环里，每次重连都 new 一个；而旧线程的退出条件
        # `not self.stop and self.ws is not None` 依赖 self.ws 变 None 才结束，
        # 可 self.ws 只会被【重新赋值】、从不置 None ⇒ 旧线程永不退出，
        # 且它读的是 self.ws 属性 ⇒ 重连后 N 个线程挤在新连接上发 N 倍 ping，无上限累积。
        threading.Thread(target=self._heartbeat_loop, daemon=True,
                         name="wecom-heartbeat").start()
        # 等待回执的超时扫描，同样【只起这一个】（理由见 _pending_sweeper）。
        threading.Thread(target=self._pending_sweeper, daemon=True,
                         name="wecom-pending-sweeper").start()
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
                # blocked until close
                self.ws.run_forever(ping_interval=0)
            except Exception as e:
                _log(f"[ws] run_forever 异常: {e}")
            if self.stop:
                break
            _log(f"[ws] {self._backoff}s 后重连")
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def _heartbeat_loop(self):
        # 常驻单线程：只在「已订阅」期间发 ping。断开时 _on_close 会 clear subscribed，
        # 它自然静默等待重连，无需也不该再起第二个线程。
        while not self.stop:
            if self.subscribed.is_set():
                self._send({"cmd": "ping", "headers": {"req_id": self._new_req_id()}})
                self._last_hb = self._now()
            time.sleep(HEARTBEAT_SEC)

    def stop_bot(self):
        self.stop = True
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
