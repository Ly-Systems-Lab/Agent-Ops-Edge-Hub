# SPDX-License-Identifier: Apache-2.0
"""一条命令的完整生命周期：建连 → 订阅 → 等 SUBACK → 发布 → 等 id 配对回执 → 断开。

设计上刻意保持**无状态**：每次调用一条新连接，不排队、不重试、不缓存。
理由见 README「五条实装约束」—— 尤其是「设备侧不按 id 去重」那一条。
"""
from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from . import errors
from .config import Config

# 终态判定：有 `status` 就认 done / error；**没有 status 字段的设备视为只回一次**
# ⇒ 收到即终态。规则里不写任何具体设备名 —— 接自己的设备时不用改这里。
#
# ⚠️ 2026-11-03 实测：车在 MQTT 上**也只有一条终态 ack**（spin 2.4 s / showoff 22 s），
#    「先回已受理、再补 done」说的是 car_bot 发给企微的那条消息，不进总线。
TERMINAL_STATUS = ("done", "error")

# 建连（TCP + TLS + CONNACK）单独一个预算，**不与 --timeout 共用**。
# 共用会得到一个荒谬的结果：用户把等待调小想快速失败，结果连证书握手都来不及做完，
# 报出来的还是「连不上 broker」—— 分类就错了。
CONNECT_TIMEOUT = 8.0


@dataclass
class Result:
    stage: str
    ok: bool
    device: str
    cmd: str
    id: str
    topic: str = ""
    ack_topic: str = ""
    rtt_ms: float = 0.0
    ack: dict | None = None
    interims: list = field(default_factory=list)
    detail: str = ""
    connack: object = None
    suback: list | None = None

    @property
    def exit_code(self) -> int:
        return errors.EXIT_CODES.get(self.stage, 1)


class _State:
    """回调线程与主线程之间唯一的共享对象。

    ⚠️ paho 2.x **不捕获回调里的异常**：抛出去只会让网络线程静默死掉，
    进程还活着但再也收不到消息。所以每个回调都包了 try/except，把异常记在这里。
    """

    def __init__(self) -> None:
        self.connected = threading.Event()
        self.subacked = threading.Event()
        self.connack = None
        self.suback = None
        self.acks: list[dict] = []
        self.bad_payloads: list[str] = []
        self.callback_errors: list[str] = []

    def guard(self, name, fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 —— 绝不能让它跑到网络线程上
                self.callback_errors.append("%s: %s: %s" % (name, type(e).__name__, e))
                return None

        return wrapper


def _reason(value):
    """paho 2.x 的 ReasonCode 不能 int()，取 .value。"""
    return getattr(value, "value", value)


def _is_terminal(ack: dict) -> bool:
    """这条 ack 是不是终态？**只看报文，不看设备名** —— 换设备不用改这里。"""
    if ack.get("ok") is False:
        return True
    status = ack.get("status")
    if status is None:
        # 没有 status 字段 ⇒ 这台设备只回一次，收到即终态
        return True
    return status in TERMINAL_STATUS


def _make_client(cfg: Config) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="winagent-%s" % uuid.uuid4().hex[:8],
    )
    client.username_pw_set(cfg.user, cfg.password)
    client.tls_set(
        ca_certs=str(cfg.ca),
        certfile=str(cfg.cert),
        keyfile=str(cfg.key),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    # 下限抬到 1.2（与 broker 的 tls_version tlsv1.2 对齐）；上限自动到 1.3
    client._ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    # ★ 主机名校验一定要开：证书 SAN 里带了设备 IP，IP 变了就该失败
    #   —— 那是信号，不是要绕开的障碍（绕开等于放弃防中间人）
    client._ssl_context.check_hostname = True
    return client


def send(
    cfg: Config,
    device: str,
    cmd: str,
    arg: str | None = None,
    timeout: float = 8.0,
    wait_final: bool = False,
    final_timeout: float = 40.0,
    command_id: str | None = None,
) -> Result:
    topic = "hub/command/%s" % device
    ack_topic = topic + "/ack"
    cid = command_id or "win-%s" % uuid.uuid4().hex[:10]

    payload = {"v": 1, "id": cid, "cmd": cmd}
    if arg:
        payload["arg"] = arg

    def result(stage, detail="", **kw):
        return Result(stage=stage, ok=(stage == errors.OK), device=device, cmd=cmd,
                      id=cid, topic=topic, ack_topic=ack_topic, detail=detail, **kw)

    st = _State()
    client = _make_client(cfg)

    def on_connect(cli, userdata, flags, rc, properties=None):
        st.connack = _reason(rc)
        st.connected.set()
        if st.connack == 0:
            cli.subscribe(ack_topic, qos=1)

    def on_subscribe(cli, userdata, mid, granted, properties=None):
        st.suback = [_reason(g) for g in granted]
        st.subacked.set()

    def on_message(cli, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            st.bad_payloads.append(msg.payload[:120].decode("utf-8", "replace"))
            return
        if not isinstance(data, dict):
            # 合法 JSON 但不是对象（[] / 123 / "x"）—— 下一行 .get() 就会炸
            st.bad_payloads.append(repr(data)[:120])
            return
        if data.get("id") != cid:
            return          # 别人的回执，不认
        st.acks.append(data)

    client.on_connect = st.guard("on_connect", on_connect)
    client.on_subscribe = st.guard("on_subscribe", on_subscribe)
    client.on_message = st.guard("on_message", on_message)

    try:
        client._connect_timeout = CONNECT_TIMEOUT
    except Exception:
        pass

    # ---- 1. 建连 ---------------------------------------------------------
    try:
        client.connect(cfg.host, cfg.port, keepalive=30)
    except ssl.SSLCertVerificationError as e:
        return result(errors.TLS, "证书校验失败：%s" % e)
    except ssl.SSLError as e:
        return result(errors.TLS, "TLS 握手失败：%s" % e)
    except (socket.timeout, TimeoutError):
        return result(errors.CONNECT, "TCP 连接超时（%s:%d）" % (cfg.host, cfg.port))
    except OSError as e:
        return result(errors.CONNECT, "连不上 %s:%d —— %s" % (cfg.host, cfg.port, e))

    client.loop_start()
    try:
        # ---- 2. 等 CONNACK ----------------------------------------------
        if not st.connected.wait(CONNECT_TIMEOUT):
            return result(errors.CONNECT, "等了 %0.0fs 没有 CONNACK" % CONNECT_TIMEOUT,
                          connack=None)
        if st.connack != 0:
            return result(errors.CONNECT, "CONNACK 拒绝（reason=%s）" % (st.connack,),
                          connack=st.connack)

        # ---- 3. 等 SUBACK（★ 铁律：拿到 SUBACK 才发，不靠 sleep 猜）-------
        if not st.subacked.wait(5.0):
            return result(errors.SUBSCRIBE, "5s 内没有 SUBACK", connack=st.connack)

        # ---- 4. 发布 -----------------------------------------------------
        t0 = time.monotonic()
        info = client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return result(errors.CONNECT, "publish 失败 rc=%s" % info.rc,
                          suback=st.suback)
        # PUBACK 只代表 broker 收下了，**不代表 ACL 放行**，更不代表设备收到了
        info.wait_for_publish(timeout=min(3.0, max(0.5, timeout)))

        # ---- 5. 等配对回执 ------------------------------------------------
        deadline = t0 + timeout
        while time.monotonic() < deadline and not st.acks:
            time.sleep(0.01)

        if not st.acks:
            return result(errors.TIMEOUT, "%.1fs 内没有 id 匹配的回执" % timeout,
                          connack=st.connack, suback=st.suback,
                          rtt_ms=(time.monotonic() - t0) * 1000.0)

        # ---- 6. 可选：等终态（小车"受理 → done"）-------------------------
        if wait_final:
            fin_deadline = t0 + final_timeout
            while time.monotonic() < fin_deadline:
                if _is_terminal(st.acks[-1]):
                    break
                time.sleep(0.02)
            if not _is_terminal(st.acks[-1]):
                return result(
                    errors.TIMEOUT,
                    "只收到受理回执，%.0fs 内没等到终态（done/error）" % final_timeout,
                    ack=st.acks[-1], interims=st.acks[:-1],
                    rtt_ms=(time.monotonic() - t0) * 1000.0,
                    connack=st.connack, suback=st.suback,
                )

        final = st.acks[-1] if wait_final else st.acks[0]
        interims = st.acks[:-1] if wait_final else []
        rtt = (time.monotonic() - t0) * 1000.0

        if final.get("ok") is False:
            return result(errors.DEVICE_ERROR,
                          "设备回 ok=false：%s" % (final.get("error") or final.get("status") or "未说明"),
                          ack=final, interims=interims, rtt_ms=rtt,
                          connack=st.connack, suback=st.suback)

        return result(errors.OK, "", ack=final, interims=interims, rtt_ms=rtt,
                      connack=st.connack, suback=st.suback)

    finally:
        # 先 disconnect 再 loop_stop：反过来的话 DISCONNECT 包发不出去，
        # broker 要等 keepalive 超时才回收这条连接
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass


def tls_probe(cfg: Config) -> dict:
    """裸 TLS 握手，拿协商版本 / 密码套件 / broker 证书信息。

    单独做一次，是为了让 `doctor` 能把「证书到底对不对」单独说清楚 ——
    混在 MQTT 失败里，用户只能看到一句"连不上"。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(cfg.ca))
    ctx.load_cert_chain(certfile=str(cfg.cert), keyfile=str(cfg.key))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True

    with socket.create_connection((cfg.host, cfg.port), timeout=8.0) as sock:
        with ctx.wrap_socket(sock, server_hostname=cfg.host) as tls:
            peer = tls.getpeercert()

            def flat(rdns):
                out = []
                for rdn in rdns or ():
                    for item in rdn:
                        if isinstance(item, (tuple, list)) and len(item) == 2:
                            out.append("%s=%s" % (item[0], item[1]))
                        else:
                            out.append(str(item))
                return ", ".join(out)

            return {
                "version": tls.version(),
                "cipher": tls.cipher()[0] if tls.cipher() else "",
                "subject": flat(peer.get("subject")),
                "issuer": flat(peer.get("issuer")),
                "san": ["%s:%s" % (k, v) for k, v in peer.get("subjectAltName", ())],
                "not_before": peer.get("notBefore", ""),
                "not_after": peer.get("notAfter", ""),
                "hostname_ok": True,
            }