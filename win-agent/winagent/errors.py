# SPDX-License-Identifier: Apache-2.0
"""失败分类 —— 每条失败都必须能指出「断在哪一环」。

为什么不做成一个泛泛的 `failed`：本仓的规约要求「拿不到回执就如实报错，
不能默认成功」。而"如实"的前提是**能分开**：连不上 broker、订阅没生效、
设备没回、设备回了但执行失败 —— 这四种的处置方式完全不同。
"""
from __future__ import annotations

# 失败阶段（stage）
CONFIG = "config"          # 本地配置 / 证书文件缺失
TLS = "tls"                # 握手失败（证书过期、CA 不对、SAN 不含该 IP）
CONNECT = "connect"        # TCP 或 CONNACK 失败（用户名密码错、broker 没开）
SUBSCRIBE = "subscribe"    # 订阅没拿到 SUBACK
TIMEOUT = "timeout"        # 订上了、发出去了，但超时没有 id 配对
DEVICE_ERROR = "device"    # 设备回了 ack，但 ok=false
OK = "ok"

# 退出码：shell 里 `&&` / `||` 能直接分流
EXIT_CODES = {
    OK: 0,
    CONFIG: 2,
    TLS: 3,
    CONNECT: 4,
    SUBSCRIBE: 5,
    TIMEOUT: 6,
    DEVICE_ERROR: 7,
}

# 人能看懂的提示（不是给机器的，别塞进 JSON）
HINTS = {
    CONFIG: "本地配置或证书缺失 —— 检查 .env 与 ca.crt / <客户端>.crt / <客户端>.key",
    TLS: "TLS 握手失败 —— 证书过期、CA 不匹配，或 broker 证书的 SAN 不含当前 IP",
    CONNECT: "连不上 broker —— 检查 IP / 端口 / 用户名密码；CONNACK 非 0 通常是凭据错误",
    SUBSCRIBE: "订阅 ack 主题没拿到 SUBACK —— broker 异常或连接被立即关闭",
    TIMEOUT: "超时：命令发出去了，但没有收到 id 匹配的回执。"
             "可能设备离线、设备没订阅命令主题，或被 ACL 静默丢弃",
    DEVICE_ERROR: "设备收到并处理了命令，但执行失败（error 字段见 ack）",
}


class ConfigError(Exception):
    """本地配置问题（缺键、文件不存在）。"""