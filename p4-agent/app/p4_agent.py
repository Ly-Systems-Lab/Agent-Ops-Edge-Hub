#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P4 控制面板 ↔ 树莓派 Agent 适配器

依据契约：docs/P4-PI-AGENT-SPEC.md（本仓内）

职责：
  1. 订阅 hub/chat/p4，收到用户指令
  2. 调本机 OpenClaw Gateway 的 /v1/chat/completions（流式）
  3. 把回复按契约切片，流式发到 hub/chat/p4/reply
  4. 按 id 去重、保活帧、60s 超时规则、错误码

硬约束（违反会在 P4 屏幕上表现为"看不懂"）：
  - 单帧 ≤ 4 KB UTF-8 字节
  - id 逐字符回传
  - final:true 恰好一次
  - 每 20s 至少一帧（保活）
  - hub/chat/* 不设 retained
  - 无 Markdown / 无 emoji
"""

import json
import os
import queue
import re
import ssl
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt

# ---------------- 配置 ----------------
DEVICE_ID = os.environ.get("P4_DEVICE_ID", "p4")
TOPIC_PREFIX = os.environ.get("P4_TOPIC_PREFIX", "hub")

MQTT_HOST = os.environ.get("P4_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("P4_MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("P4_MQTT_USER", "agent")
MQTT_PASS = os.environ.get("P4_MQTT_PASS", "")
# 证书路径默认相对本文件解析（<repo>/p4-agent/certs/），换部署目录无需改代码
_CERTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs")
CA_FILE = os.environ.get("P4_CA_FILE", os.path.join(_CERTS, "ca.crt"))
# 每个客户端一张独立证书（双向认证的要求，出问题可单点吊销）
CERT_FILE = os.environ.get("P4_AGENT_CERT", os.path.join(_CERTS, "agent.crt"))
KEY_FILE = os.environ.get("P4_AGENT_KEY", os.path.join(_CERTS, "agent.key"))

GATEWAY_URL = os.environ.get("P4_GATEWAY_URL", "http://127.0.0.1:18789/v1/chat/completions")
GATEWAY_TOKEN = os.environ.get("P4_GATEWAY_TOKEN", "")
GATEWAY_MODEL = os.environ.get("P4_GATEWAY_MODEL", "openclaw/main")

# 契约 §5.3/§5.5
MAX_FRAME_BYTES = 4096          # 硬上限，超了 P4 整条丢弃
SAFE_FRAME_BYTES = 3500         # 我方自留余量
KEEPALIVE_SEC = 20              # 每 20s 至少一帧
HARD_TIMEOUT_SEC = 150          # 硬超时（P4 是 60s 无帧超时，我们保活所以能续）
DEDUP_CAPACITY = 200            # 契约 §5.4 第 5 条
# ★ 严格串行：P4 屏上只有一个气泡，并发两条会互相抢流（实测 -0004 只有 17 帧
#   却要等 -0003 的 543 帧流完才显完，用户看到的就是“排队”）。
#   P4 本身不排队（它会在上一句没回完时就发下一句），所以拒在 Pi 侧。
MAX_CONCURRENT = 1              # 同时在跑的指令上限，超过回 agent_busy

TOPIC_IN = f"{TOPIC_PREFIX}/chat/{DEVICE_ID}"
TOPIC_REPLY = f"{TOPIC_PREFIX}/chat/{DEVICE_ID}/reply"
TOPIC_STATUS = f"{TOPIC_PREFIX}/device/{DEVICE_ID}/status"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- 文本净化（契约 §6）----------------
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U0001F000-\U0001F0FF"
    "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF"
    "\U0000FE0F" "\U0000200D" "]+",
    flags=re.UNICODE,
)


def sanitize(text):
    """去掉 emoji 与 Markdown 标记，P4 是 LVGL 纯文本 label。"""
    if not text:
        return ""
    text = _EMOJI_RE.sub("", text)
    # Markdown -> 纯文本
    text = re.sub(r"```[a-zA-Z0-9]*\n?", "", text)   # 代码块围栏
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # 标题
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)     # 粗体
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", text)  # 斜体
    text = re.sub(r"`([^`]*)`", r"\1", text)         # 行内代码
    text = re.sub(r"^\s{0,3}[-*+]\s+", "· ", text, flags=re.M)  # 列表
    text = re.sub(r"\[(.+?)\]\((.*?)\)", r"\1", text)  # 链接
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def frame_size(obj):
    """按契约计算报文的 UTF-8 字节数。ensure_ascii=False 是硬要求。"""
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


# JSON 转义后的字节数（json.dumps(..., ensure_ascii=False)）：
# 这几个字符输出成两字节的短转义，其余控制字符输出成 6 字节的 \u00XX。
_JSON_ESC_LEN = {'"': 2, "\\": 2, "\n": 2, "\r": 2, "\t": 2, "\b": 2, "\f": 2}


def _esc_len(ch):
    """一个字符在**序列化之后**占多少 UTF-8 字节。

    ★ 为什么不能直接用 len(ch.encode())（2026-10-31 修）：
      split_deltas 原先按**原始 UTF-8 字节**算预算，但真正发出去的是
      json.dumps 之后的报文 —— `"` 变 `\\"`、`\\` 变 `\\\\`（各 +1），
      控制字符变 `\\u00XX`（+5）。于是"预算 3500 字节"的切片实际能到 7000+，
      撞上 _raw_send 的 4096 硬上限 ⇒ 整条回复被降级成 internal 错误帧而**中止**。
      典型触发：回复里贴代码 / 贴 JSON（双引号很多）。
      ⇒ 预算必须按"最终序列化之后的字节"算。
    """
    n = _JSON_ESC_LEN.get(ch)
    if n is not None:
        return n
    o = ord(ch)
    if o < 0x20:
        return 6            # \u00XX
    if o < 0x80:
        return 1
    if o < 0x800:
        return 2
    if o < 0x10000:
        return 3
    return 4                # 星平面（ensure_ascii=False 原样输出，按 UTF-8 算）


def split_deltas(text, base_overhead):
    """把整段文本切成若干 delta，保证**拼出来的每帧 ≤ SAFE_FRAME_BYTES**。

    base_overhead 由调用方用 frame_size() 算：不含 delta 内容的整帧开销。
    这里按"每个字符序列化后的字节数"累加，所以切出来的帧即使写满预算也不会超。
    """
    budget = SAFE_FRAME_BYTES - base_overhead
    if budget < 200:
        budget = 200
    out, cur, used = [], [], 0
    for ch in text:
        n = _esc_len(ch)
        if cur and used + n > budget:
            out.append("".join(cur))
            cur, used = [ch], n
        else:
            cur.append(ch)
            used += n
    if cur:
        out.append("".join(cur))
    return out or [""]


# ---------------- 去重（契约 §5.4 第 5 条）----------------
class Dedup:
    def __init__(self, capacity=DEDUP_CAPACITY):
        self.cap = capacity
        self.order = []
        self.seen = {}

    def check_and_mark(self, key):
        """True = 首次见（应执行）；False = 重复（应忽略）"""
        now = time.time()
        if key in self.seen:
            return False
        self.seen[key] = now
        self.order.append(key)
        while len(self.order) > self.cap:
            old = self.order.pop(0)
            self.seen.pop(old, None)
        return True

    def contains(self, key):
        """只查**不标记**。给"忙"分支用（见 handle 里的说明）。"""
        return key in self.seen


# ---------------- 回执发送器 ----------------
class Reply:
    def __init__(self, client, msg_id):
        self.client = client
        self.id = msg_id
        self.seq = 0
        self.closed = False
        self.offline = False
        self.lock = threading.Lock()
        self.last_sent = time.time()

    def _tag(self):
        """日志用：id 太长时裁短，避免刷屏。"""
        i = self.id
        return f"id={i}" if len(i) <= 48 else f"id={i[:24]}…({len(i)}字)"

    def _raw_send(self, payload):
        """真正发一帧；返回是否成功。不做任何自愈，调用方负责。"""
        raw = json.dumps(payload, ensure_ascii=False)
        n = len(raw.encode("utf-8"))
        if n > MAX_FRAME_BYTES:
            log(f"[WARN] 帧超限 {n}B（上限 {MAX_FRAME_BYTES}B）seq={self.seq}")
            return False
        try:
            self.client.publish(TOPIC_REPLY, raw, qos=1, retain=False)
        except Exception as e:
            # 链路层异常：裁短/换 msg 都救不了，标记后交给调用方一次性放弃
            self.offline = True
            log(f"[WARN] publish 异常 seq={self.seq}: {e}")
            return False
        self.last_sent = time.time()
        return True

    def _error_frame(self, code, msg, seq):
        """构造最小错误帧；msg 裁短以保证一定能塞进 4KB。"""
        return {"v": 1, "id": self.id, "seq": seq, "final": True,
                "error": {"code": code, "msg": msg}}

    def _publish_final_error(self, code, msg):
        """★ 发终结错误帧。调用方已持锁且 closed=False。

        尺寸自愈：先按原样试；若仍超限（极长 id 才有），逐级裁短 msg，
        最后退到只用 code。保证「一定有一个 final 帧送到」，不会静默卡死。
        实在连最小帧都发不出（id 本身超 4KB），只能记日志——那时 P4 也配不上对。
        """
        for m in (msg, "内部错误（报文超限）", "internal", ""):
            if self._raw_send(self._error_frame(code, m, self.seq)):
                self.seq += 1
                self.closed = True
                log(f"[error] {self._tag()} code={code} msg={m!r}（已送达）")
                return True
            if self.offline:      # 链路断了，别再试
                break
        self.closed = True
        log(f"[FATAL] 连最小错误帧都发不出（id {len(self.id)} 字），"
            f"P4 侧该气泡将卡到超时 {self._tag()}")
        return False

    def _publish(self, payload):
        """友好路径：普通 delta / final 帧。超限则降级为 internal 错误帧。"""
        if self._raw_send(payload):
            return True
        # 超限或 publish 异常 —— 不能静默丢，否则 P4 会一直等到 60s 超时
        return self._publish_final_error("internal", "回复内容过长，已中止")

    def delta(self, text):
        with self.lock:
            if self.closed:
                return
            if self._publish({"v": 1, "id": self.id, "seq": self.seq,
                              "delta": text, "final": False}):
                self.seq += 1

    def keepalive(self):
        self.delta("")

    def final(self, text=""):
        with self.lock:
            if self.closed:
                return
            if text:
                if self._publish({"v": 1, "id": self.id, "seq": self.seq,
                                  "delta": text, "final": False}):
                    self.seq += 1
                if self.closed:      # 上一步可能已降级成错误终结
                    return
            if self._publish({"v": 1, "id": self.id, "seq": self.seq,
                              "delta": "", "final": True}):
                self.seq += 1
            self.closed = True

    def error(self, code, msg):
        with self.lock:
            if self.closed:
                return
            self._publish_final_error(code, msg)


# ---------------- 保活看门狗 ----------------
class Keepalive(threading.Thread):
    def __init__(self, reply):
        super().__init__(daemon=True)
        self.reply = reply
        self.stop_evt = threading.Event()

    def run(self):
        while not self.stop_evt.wait(2.0):
            if self.reply.closed:
                return
            if time.time() - self.reply.last_sent >= KEEPALIVE_SEC:
                log(f"[keepalive] id={self.reply.id} seq={self.reply.seq}")
                self.reply.keepalive()

    def stop(self):
        self.stop_evt.set()


# ---------------- 调 Gateway ----------------
def call_agent(text, conv_id, reply):
    """
    流式调 OpenClaw Gateway，边收边发。
    返回 (ok, errcode, errmsg)
    """
    body = json.dumps({
        "model": GATEWAY_MODEL,
        "user": conv_id,
        "stream": True,
        "messages": [{"role": "user", "content": text}],
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(GATEWAY_URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {GATEWAY_TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")

    started = time.time()
    sent_any = False
    try:
        with urllib.request.urlopen(req, timeout=HARD_TIMEOUT_SEC) as r:
            for raw in r:
                if reply.closed:
                    break
                if time.time() - started > HARD_TIMEOUT_SEC:
                    return False, "agent_timeout", "Agent 响应超时"
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue        # SSE 里混进非对象片段：跳过，别让整条流崩掉
                ch = (obj.get("choices") or [{}])[0]
                piece = (ch.get("delta") or {}).get("content") or ""
                if not piece:
                    continue
                for d in split_deltas(sanitize(piece), frame_size(
                        {"v": 1, "id": reply.id, "seq": 99999, "delta": "", "final": False})):
                    reply.delta(d)
                    sent_any = True
        return True, None, None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        log(f"[gateway] HTTP {e.code}: {detail}")
        return False, "internal", f"Agent 返回 {e.code}"
    except (TimeoutError, urllib.error.URLError) as e:
        log(f"[gateway] 超时/连接失败: {e}")
        return False, "agent_timeout", "Agent 超时未响应"
    except Exception as e:
        log(f"[gateway] 异常: {e}\n{traceback.format_exc()}")
        return False, "internal", "内部错误"


# ---------------- 主流程 ----------------
dedup = Dedup()
# 并发闸：P4 实际是一次一句，这里只是防"连点/重发"把 Gateway 打满
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def handle(client, payload):
    # ★ 严格串行闸（MAX_CONCURRENT=1）：P4 屏上只有一个气泡。
    #   实测：P4 并不会等上一句回完才发下一句（-0003 还在流式时就发出了 -0004），
    #   两者并发会互相抢流，用户看到的就是「排队」。既然物理形态是一次一句，
    #   就在 Pi 侧直接拒掉后来者（agent_busy 是 SPEC §5.3 允许的四个错误码之一）。
    #
    #   注意：拒绝发生在 dedup 标记之前，所以**新 id** 被拒时没有被标记，
    #   用户重发同一句话仍会正常执行（不会被当成重复忽略）。
    #
    #   ★ 2026-10-31 补：**同 id 的重投不能走这条路**。
    #   它要么正在跑（seen 里有），要么已经跑完（仍在去重窗口内）——
    #   两种情况都必须静默忽略。否则这里会新建一个 Reply(mid) 从 seq=0 发
    #   final:true 的 agent_busy 帧，而同 id 的原流还在往后发 delta
    #   ⇒ 同一 id 两条流、**两个 final**，直接违反契约"final:true 恰好一次"，
    #   P4 侧表现为提前结束 + 结束后还继续蹦正文。
    if not _slots.acquire(blocking=False):
        try:
            req = json.loads(payload)
        except Exception:
            req = None
        mid = req.get("id") if isinstance(req, dict) else None
        if not isinstance(mid, str) or not mid:
            log("[busy] 已有指令在跑，拒绝无 id 报文")
            return
        if dedup.contains(mid):
            log(f"[busy] 同 id 重投 id={mid}，静默忽略（不重复执行、不发错误帧）")
            return
        log(f"[busy] 已有指令在跑，拒绝 {mid}")
        Reply(client, mid).error("agent_busy", "当前忙，请稍后再发")
        return
    try:
        _handle_inner(client, payload)
    finally:
        _slots.release()


def _handle_inner(client, payload):
    # ---- 校验 ----
    try:
        req = json.loads(payload)
    except Exception:
        log(f"[recv] 非法 JSON: {payload[:120]!r}")
        return
    # 合法 JSON 但不是对象（[] / 123 / "x"）⇒ 下一行 .get() 就 AttributeError，
    # 而异常会冲出线程（daemon，日志里一堆 traceback 但进程照旧运行）。
    if not isinstance(req, dict):
        log(f"[recv] 不是 JSON 对象（{type(req).__name__}），丢弃: {payload[:120]!r}")
        return
    msg_id = req.get("id")
    if not isinstance(msg_id, str) or not msg_id:
        log(f"[recv] 缺 id: {req}")
        return

    if req.get("v") != 1:
        Reply(client, msg_id).error("bad_request", "协议版本必须是 1")
        return

    text = req.get("text")
    if not isinstance(text, str) or not text.strip():
        Reply(client, msg_id).error("bad_request", "指令内容为空")
        return

    # ---- 去重 ----
    if not dedup.check_and_mark(msg_id):
        log(f"[dedup] 重复 id={msg_id}，忽略（不重复执行）")
        return

    log(f"[recv] id={msg_id} text={text[:60]!r}")
    reply = Reply(client, msg_id)
    ka = Keepalive(reply)
    ka.start()
    try:
        # ★ 会话 ID 必须稳定，不能绑 msg_id（否则每句一个新会话，多轮上下文全丢）
        conv = f"conv:{DEVICE_ID}-main"
        # ★ 2026-10-31：把「总截止时间」做成**真的硬**的。
        #   call_agent 里的 urllib timeout 是**每次 socket 读**的超时，而
        #   `for raw in r` 是阻塞读 —— 第 149 秒来了一行之后还会再阻塞 150 秒，
        #   最坏能到 ~299 秒。这期间保活线程每 20s 喂一帧（P4 不会自己超时），
        #   而 _slots 一直被占着 ⇒ 后面每条指令都回 agent_busy。
        #   ⇒ 整个调用丢进工作线程，主流程只等 HARD_TIMEOUT_SEC；到点就发
        #     agent_timeout 并放槽位。被丢下的线程是 daemon，它的 reply.delta()
        #     会因 reply.closed 变成空操作，不会往屏幕上再写东西。
        # ★ 2026-10-31：用 queue.Queue(1) 传结果，不再用裸 dict。
        #   dict 靠 Event 做同步虽然可行，但那是「隐式内存序」；Queue 自带锁，
        #   语义明确，也不会出现「线程还在写、主线程已经读」的时序想象空间。
        box = queue.Queue(maxsize=1)
        done = threading.Event()

        def _worker():
            try:
                box.put((True, call_agent(sanitize(text), conv, reply)))
            except BaseException as e:      # 工作线程里也不许静默地死掉
                box.put((False, (False, "internal", f"内部错误: {e}")))
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True,
                         name=f"p4-call-{msg_id[:12]}").start()

        if not done.wait(HARD_TIMEOUT_SEC):
            log(f"[timeout] id={msg_id} 超过硬上限 {HARD_TIMEOUT_SEC}s，强制结束并放槽位")
            reply.error("agent_timeout", "Agent 响应超时")
            # ★ 2026-10-31：显式置位 closed，不依赖 reply.error() 内部去关。
            #   被丢下的工作线程仍可能在稍后拿到 SSE 行并调 reply.delta()，
            #   而 delta() 的唯一闸门就是 self.closed。若 error() 的实现将来变了
            #   （比如异常路径没走到 _publish_final_error 的 self.closed = True），
            #   残留线程就会继续往 P4 屏幕上写东西。这里补一道独立保险。
            reply.closed = True
            return

        try:
            _, (ok, code, msg) = box.get_nowait()
        except queue.Empty:
            ok, code, msg = False, "internal", "内部错误（工作线程未回传结果）"
        if not ok:
            if code == "agent_timeout":
                reply.error("agent_timeout", msg)
            else:
                reply.error(code or "internal", msg or "内部错误")
        else:
            reply.final()
        log(f"[done] id={msg_id} frames={reply.seq} ok={ok}")
    except Exception as e:
        log(f"[handle] 崩溃: {e}\n{traceback.format_exc()}")
        try:
            reply.error("internal", "内部错误")
        except Exception:
            pass
    finally:
        ka.stop()


# ---------------- MQTT ----------------
def make_client():
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"p4-agent-{int(time.time())%10000}",
                    clean_session=True, protocol=mqtt.MQTTv311)
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_FILE)
    ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    ctx.check_hostname = False       # 内网 IP 会变，靠 CA 校验链即可
    ctx.verify_mode = ssl.CERT_REQUIRED
    c.tls_set_context(ctx)
    c.tls_insecure_set(True)         # 允许 IP 直连（证书 SAN 可能不含新 IP）
    c.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(cli, u, flags, rc, props=None):
        if rc != 0:
            log(f"[mqtt] 连接失败 rc={rc}")
            return
        cli.subscribe(TOPIC_IN, qos=1)
        log(f"[mqtt] 已连 {MQTT_HOST}:{MQTT_PORT}，订阅 {TOPIC_IN}")

    def on_message(cli, u, msg):
        threading.Thread(target=handle, args=(cli, msg.payload), daemon=True).start()

    def on_disconnect(cli, u, flags, rc, props=None):
        log(f"[mqtt] 断开 rc={rc}（自动重连中）")

    c.on_connect = on_connect
    c.on_message = on_message
    c.on_disconnect = on_disconnect
    c.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    return c


def main():
    if not MQTT_PASS:
        log("错误：缺少 P4_MQTT_PASS 环境变量")
        sys.exit(2)
    if not GATEWAY_TOKEN:
        log("错误：缺少 P4_GATEWAY_TOKEN 环境变量")
        sys.exit(2)
    c = make_client()
    log(f"[start] agent 启动，device={DEVICE_ID} prefix={TOPIC_PREFIX}")

    # ★ systemd 的 stop/restart 发的是 SIGTERM。paho 的 loop_forever() 不会自己响应它，
    #   不注册处理器会导致 systemctl stop 一直卡到超时（TimeoutStopSec），
    #   再被 SIGKILL——日志里表现为"Deactivated successfully"但进程其实拖了很久。
    shutdown_evt = threading.Event()

    def _on_sig(signum, frame):
        log(f"[main] 收到信号 {signum}，准备退出")
        shutdown_evt.set()

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    try:
        # loop_start + 自己等事件，这样能被信号唤醒；重连由 paho 自己处理
        c.loop_start()
        while not shutdown_evt.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        log("[main] 退出")
        try:
            c.disconnect()
            c.loop_stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
