# 曼波语音小车插件

把「曼波语音小车」接进 Pi Hub 的企微入口。

```
企微 @曼波小车 转一圈
        │
        ▼
   bot.py（WeComBot 子类，凭据来自 .env 的 CAR_WECOM_BOT_*）
        │  publish
        ▼
   hub/command/car          ← 小车侧（独立的车控机）订阅
        │  执行动作
        ▼
   hub/command/car/ack      ← 车侧回执（QoS0，无 retain）
        │  subscribe
        ▼
   bot.py 按 id 配对 → 回企微
```

**与 P4 的应答差异（有意为之）**：小车动作很长（「露一手」≈ 转 3 圈 + 21 s 音乐），
所以 **收到即回「已受理」**，不等动作跑完；动作结束后车侧补一条 `done` 回执走异步通知。
不要照 P4 的「同步等 5 秒回执」来改这里。

## 目录内容

| 文件 | 作用 |
|---|---|
| `bot.py` | 企微机器人（继承 `core/wecom_base.py` 的 `WeComBot`） |
| `command.py` | 中文指令 → `{cmd}` 解析 + 回执措辞 |
| `systemd/pihub-car-bot.service` | 常驻单元 |
| `.env`（仓外） | `CAR_WECOM_BOT_ID` / `CAR_WECOM_BOT_SECRET` |

凭据**独立于**小电视与 P4 那两对，各建一个企微机器人，不要复用。

## 接入要点

- 车侧是**独立机器**（自己的客户端，连 broker 的 `8883` TLS + mTLS，账号 `car`）
- ACL：`car` 只 `read hub/command/car`、`write hub/command/car/ack` + `hub/device/car/status`
  —— **它不能写 `hub/command/car`**，这是刻意的（设备身份只收命令、只发回执）
- 本机 `bot.py` 用 `pihub` 账号（1883 回环）发布，这是唯一有写权限的那一侧

## ⚠ 关于 `car_gateway.py`（存在但**不要参照部署**）

实机 `/home/pi/cooperate/pihub/devices/car/car_gateway.py` 曾有一个「8883 → 1883 转发」网关，
理由是「回环订阅者收不到 8883 客户端发布的消息」。

**这个前提 2026-11-23 已实测推翻**：本 broker 的两个 listener **双向互通**，
消息经同一 broker 内部路由，`per_listener_settings true` 只影响「认证/ACL 每个 listener 各写一遍」，
**不隔离消息投递**。

```
实测（用有 ACL 权限的主题）：
  8883 发 hub/command/car/ack   → 1883 订阅者收到 ✅
  1883 发 hub/command/car       → 8883 订阅者收到 ✅
```

⇒ 该网关**不是必需组件**，本目录**不收录它**，也不提供 `cargw` ACL 角色。
若你的部署真的需要跨 broker（不是跨 listener）转发，再单独设计，别复活这个版本。

> 🩸 这个网关的诞生踩了一个经典坑：当时用 `car` 账号往 `probe/cross` 这类**无写权限的主题**发消息，
> broker 静默丢弃（PUBACK RC:0、退出码 0、日志不记 denied），于是误判成「listener 隔离」。
> 详见仓库 `TOOLS.md` / `docs/SECURITY.md` 里「ACL 静默丢弃」那条。
