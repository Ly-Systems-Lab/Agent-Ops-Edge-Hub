# P4 Agent 适配器

把 ESP32-P4 控制面板接进树莓派上的 OpenClaw Agent 的桥接进程。

```
ESP32-P4 ──mTLS──▶ MQTT hub/chat/p4
                        │
                  p4_agent.py 订阅
                        │  HTTP POST  stream:true
                        ▼
            OpenClaw Gateway /v1/chat/completions
                        │  SSE 流式
                        ▼
            MQTT hub/chat/p4/reply ──▶ P4 屏幕
```

**它做了什么**：收 MQTT 指令 → 调本机 Gateway 流式接口 → 把回复切成 ≤4KB 的帧发回去，
带保活、去重、超时、错误码。P4 那边只是个 LVGL 纯文本 label，所以还要剥 Markdown / emoji。

---

## 为什么单独一个目录

`pihub/` 里的 4 个服务是**通用中控**（行情/天气/设备指令/企微推送），跟具体哪台设备无关。
`p4-agent/` 是**一个具体设备**的适配器：它的配置、证书、systemd 单元都只服务 P4 这一台。

分开的好处：
- P4 私有证书不进 `pihub/`，泄露面更小
- 换设备/加设备时，`pihub/` 一行不动
- P4 适配器出问题不影响中控

> ⚠️ **这正是当前底座的能力边界**：`pihub/` 是**单设备**结构
> （`hub/command/smalltv` 硬编码，`smalltv_ip` 单值）。
> 每接一台新设备，就要像 `p4-agent/` 这样再写一个适配器。
>
> **但这不是缺陷，是设计选择。** 本项目是「定制端侧接入 + Agent 互联」的结合体，
> **只有「企微机器人分层管理」那一层是通用的**。
> 刻意不做多设备路由的理由，以及「怎么接你自己的设备」，见
> [`docs/ARCHITECTURE.md` §架构边界](../docs/ARCHITECTURE.md#四架构边界这不是通用-mqtt-底座)。

---

## 文件

| 文件 | 说明 |
|---|---|
| `app/p4_agent.py` | 主程序（单文件，无本地依赖） |
| `systemd/p4-agent.service` | systemd 单元 |
| `.env.example` | 环境变量样例 |
| `certs/` | **不在仓内**（`*.key` 已被 `.gitignore` 拦），你要自己生成一套 |
| `logs/` | 运行时目录，**部署时要 `mkdir -p logs`**（unit 把日志追加到这里） |

> `app/p4_agent.py` 只依赖 `paho-mqtt`，没有本地模块 —— 但**装在哪**很关键：
> unit 用的是本目录的 `.venv`，装在 `pihub/.venv` 里的 paho 它看不见。

---

## 部署

> ⚠️ **先看路径**：`systemd/p4-agent.service` 里 4 处路径**默认写死**
> `/home/pi/cooperate/p4-agent`（与 `pihub` 那套 unit 同一约定）。
> 放到别处就得把这 4 处一起改，**漏一处服务就起不来**。

```bash
# 0) 放到约定目录
sudo mkdir -p /home/pi/cooperate
sudo cp -r p4-agent /home/pi/cooperate/
cd /home/pi/cooperate/p4-agent

# 1) 依赖：建本适配器**自己的** venv（unit 的 ExecStart 指向它）
python3 -m venv .venv && .venv/bin/pip install 'paho-mqtt==2.1.0'
mkdir -p logs          # ★ 必须建！unit 把 stdout/stderr 追加到 logs/agent.log，
                       #   目录不存在时 systemd 打不开目标、服务直接启动失败

# 2) 证书：放到 p4-agent/certs/
#    ca.crt / agent.crt / agent.key       ← 本适配器（agent 角色）
#    另需在 broker 侧装 ca.crt / broker.crt / broker.key
#    生成方式见 deploy/DEPLOY.md §5

# 3) 配置
cp .env.example .env
chmod 600 .env
#   必填：P4_MQTT_PASS（agent 账号密码）、P4_GATEWAY_TOKEN

# 4) ACL 里给 agent 角色授权（见 config/aclfile.example）
#      user agent
#      topic read  hub/chat/p4
#      topic write hub/chat/p4/reply
#      topic read  hub/device/p4/status

# 5) 装服务
sudo cp systemd/p4-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now p4-agent

# 6) 验证
systemctl is-active p4-agent
journalctl -u p4-agent -n 50      # 或 tail -f logs/agent.log
```

> **想用系统 python3 而不是 venv？** 把 unit 的 `ExecStart` 改成 `/usr/bin/python3`，
> 并且先 `sudo -H python3 -m pip install 'paho-mqtt==2.1.0'` ——
> 装在 `pihub/.venv` 里的 paho **系统 python3 拿不到**，这一点很容易搞混。

---

## 会话上下文（★ 重要）

SPEC §9 说 P4「只有一条对话流」，所以适配器给 Gateway 的会话 ID 是**固定值**：

```python
conv = f"conv:{DEVICE_ID}-main"        # = conv:p4-main
```

Gateway 的 `/v1/chat/completions` 用 `user` 字段做会话识别，
**复用同一个 `user` 值就能继续同一个 Agent 会话**（官方文档明写）：
多轮对话（“刚才那个词是什么”）、上下文指代（“把它改成 30”）都靠它。

> ⚠️ **曾经写错过（已修）**：早期版本写的是 `conv = f"conv:p4-{msg_id}"`。
> `msg_id` 每句话都不同 ⇒ **每句都是一个全新会话，完全没上下文**。
> 实测：同一 conv 连问两轮，第二轮能答出第一轮记住的“蓝鲸”；
> 换新 conv 则答“无响应”。⇒ 已改为固定 `conv:{DEVICE_ID}-main`。
>
> 副作用：所有 P4 消息共用一个 Agent 会话，历史会持续累积。
> 要清空就重启 Gateway 或换一个 `DEVICE_ID`。

## 并发限制：严格串行（`MAX_CONCURRENT = 1`）

上游是 `BoundedSemaphore(1)`。已有指令在跑时，新指令**立即**回
`agent_busy`（SPEC §5.3 允许的四个错误码之一），**不排队**。

> ⚠️ **一个被实测推翻的假设**：早期以为「P4 是一次一句，气泡期间不发下一条」。
> 实际**不是** —— P4 会在上一句还在流式时就发下一句：
>
> ```
> [00:04:15] recv  -0003  '帮我看看淄博近7天的天气吧'
> [00:04:21] recv  -0004  '你哈'          ← 上一句还没 done
> [00:04:28] done  -0003  frames=543
> [00:04:30] done  -0004  frames=17
> ```
>
> 两个线程并行时，17 帧的小回复会被 543 帧的大回复"压"到最后才显完，
> **用户看到的就是"排队"**。所以「排队」不是 P4 的设计，是抢同一气泡导致的。

**为什么不排队**：SPEC §5.4 第 3 条明写「你这边也不需要为它保留待投递队列」。

> ✅ **被拒后可原样重发**：拒绝发生在 `dedup` 之前，被拒的 `id` 没被标记，
> 所以用户重发同一句话会正常执行，不会被当成重复忽略。

## 报文契约

完整契约见 [`docs/P4-PI-AGENT-SPEC.md`](../docs/P4-PI-AGENT-SPEC.md)（§4 主题 / §5 报文），
实施交接与自验记录见 [`docs/P4-PI-HANDOFF.md`](../docs/P4-PI-HANDOFF.md)。

**五条硬约束**（违反任何一条，P4 屏幕上都会显示成"看不懂"）：

1. 单帧 ≤ **4 KB** UTF-8 字节（内部预算 3500B）
2. `id` **逐字符回传**，不解析
3. `final:true` **恰好一次**
4. 每 **20s** 至少一帧（保活用 `delta:""`）
5. `hub/chat/*` **不设 retained**

**超时**：P4 侧 60s 无帧即超时；本适配器 20s 保活一次，所以能续。硬超时 150s。

---

## 环境变量

见 [`.env.example`](./.env.example)。要点：

| 变量 | 默认 | 说明 |
|---|---|---|
| `P4_MQTT_HOST` | `127.0.0.1` | 适配器与 broker 同机，走回环 |
| `P4_MQTT_PORT` | `8883` | TLS 口。**不要**用 1883（那是本机明文口） |
| `P4_MQTT_USER` | `agent` | ACL 角色名 |
| `P4_MQTT_PASS` | — | ★ 必填 |
| `P4_CA_FILE` 等 | `<仓库>/p4-agent/certs/*` | 留空即自动相对本文件解析 |
| `P4_GATEWAY_URL` | `http://127.0.0.1:18789/v1/chat/completions` | OpenClaw Gateway |
| `P4_GATEWAY_MODEL` | `openclaw/main` | 与企微话题**同一套 Agent 能力**（能执行工具、能改文件） |
| `P4_GATEWAY_TOKEN` | — | ★ 必填 |
