#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 paho 的 on_message 加一道边界：异常只记日志，**绝不让它冲出回调**。

为什么必须有这一层（2026-10-31 补）：
    paho-mqtt 2.x **不捕获**用户回调里的异常。同一个根因，两种完全不同的表现 ——

    · 用 `loop_start()` 的进程（三个企微机器人）：
      只有**网络线程**死掉，主进程还活着。看上去一切正常（WS 还连着、心跳还在发），
      但再也收不到任何 MQTT 消息 ⇒ 设备机器人表现为"每条指令都等到超时回
      「暂无响应」"，日志里就一行 traceback，极容易被当成偶发。

    · 用 `loop_forever()` 的进程（devctl）：
      异常会抛到 main ⇒ 进程退出 ⇒ 被 systemd 的 Restart=always 拉起。
      表现为"执行器隔一会儿重连一次"，同样不像崩溃。

典型触发：总线上来了一条**合法 JSON 但不是对象**的报文
（`[]`、`123`、`"x"`），或者 `extra` 是数组而不是对象 ——
`payload.get(...)` 直接 AttributeError。正常业务不会发这种包，
但公开部署里谁都可能往总线上灌东西。

用法：
    from mqtt_guard import guard
    client.on_message = guard(on_message, _log)
"""
import functools


def guard(fn, log, tag="mqtt"):
    """把 on_message 包一层。`log` 是调用方自己的日志函数（各服务格式不同）。"""

    @functools.wraps(fn)
    def wrapper(client, userdata, msg):
        try:
            return fn(client, userdata, msg)
        except Exception as e:          # noqa: BLE001 —— 这一层就是要兜住一切
            try:
                log(f"[{tag}] 回调异常（已捕获，连接继续）: {e!r}")
            except Exception:
                pass

    return wrapper