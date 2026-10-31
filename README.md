# Pi Hub 底座

**License: Apache-2.0**

> 这是 **Agent-Ops 三件套**里的**数据面**：树莓派侧的中控底座。
> 只含 Pi 上的中控服务（Flask API / MQTT 总线 / 行情监控 / 企微 Bot / P4 与小车适配器），
> **不含** ESP32 固件与 AI Agent 实现。
>
> 三件套：控制面 `Agent-Ops-WeCom-Bridge`（企微 ⇄ Agent 网关）·
> **数据面（本仓）** · 总蓝图 `Agent-Ops-Architecture`
>
> ⚠️ **先读这段，避免误解：[架构边界](#架构边界这不是通用mqtt底座)** —— 本项目**不是**通用型 MQTT 底座，
> 端侧设备是**定制接入**的。通用的是「企微机器人分层管理」那一层。

---

## 这是什么

一句话：**企微机器人当统一入口，树莓派当中控，Agent 当大脑，端侧设备按需定制接入。**

一台树莓派做**常开中控**，把数据缓存、告警判定、设备指令、企微推送这些"边"上的活都收拢到一处：

```
                    ┌──────────────────────────────────┐
                    │        树莓派 (Pi Hub)            │
   上游行情/天气 ───▶│  monitor.py  轮询 → 告警判定      │
                    │       │                          │
                    │       ▼  hub/alert/#             │
                    │  ┌─────────┐                     │
                    │  │Mosquitto│◀── 1883 明文回环     │
                    │  │  broker │◀── 8883 TLS 局域网   │
                    │  └─────────┘                     │
                    │    │         │                   │
                    │    ▼         ▼                   │
                    │ wecom_bot  devctl  api.py(:5000) │
                    └────┬─────────┬───────────────────┘
                         │         │
                    企业微信      ESP32 设备
```

**六个常驻服务**（`pihub/` 内，systemd，全部 `Restart=always`）：

| 服务 | 脚本 | 干什么 |
|---|---|---|
| `pihub-api` | `app/api.py` | Flask HTTP 接口（:5000，**IP 白名单**），供 ESP32 与外部调用 |
| `pihub-devctl` | `app/devctl.py` | 订阅 `hub/command/smalltv`（**只这一个**），转成设备 HTTP 指令 |
| `pihub-monitor` | `app/monitor.py` | 行情轮询 → 涨跌停/异动告警 → `hub/alert/#` |
| `pihub-wecom-bot` | `app/wecom_bot.py` | 企微智能机器人双向：收消息下发指令 / 推告警（小电视） |
| `pihub-p4-bot` | `app/p4_bot.py` | P4 专属企微机器人：中文指令 → `hub/command/p4`（2026-10-28） |
| `pihub-car-bot` | `app/car_bot.py` | 小车专属企微机器人：中文指令 → `hub/command/car`（2026-10-30） |

> ⚠️ **每个机器人 = 一个独立企微账号 = 一对独立凭据**（`WECOM_*` / `P4_WECOM_*` / `CAR_WECOM_*`）。
> 复用同一对凭据会让「这条消息该谁处理」变得不可预测。

**另有一个设备适配器**（`p4-agent/`，独立目录）：

| 服务 | 脚本 | 干什么 |
|---|---|---|
| `p4-agent` | `p4-agent/app/p4_agent.py` | ESP32-P4 控制面板 ↔ OpenClaw Agent 桥接（MQTT → Gateway 流式） |

**MQTT 总线解耦**：告警、指令、回执三条流互不耦合，任一端重连不影响其它。

### 🌟 为什么 MQTT 是主线

本项目把 **MQTT（8883 / TLS + 双向证书 + ACL）作为唯一被信任的设备通道**，
HTTP（5000）只用于「MCU 没有 TLS 栈」的最小闭环场景。

**可靠性 —— 都是实装的，不是设计稿：**

| 机制 | 代码依据 |
|---|---|
| **进程级自恢复** | 全部 6 个 systemd 单元均 `Restart=always` |
| **QoS 1 全链路** | 指令下发、回执、告警 publish/subscribe 一律 `qos=1` |
| **断线重连** | paho `loop_start()` 内置自动重连；`p4-agent` 额外 `reconnect_delay_set(1, 30)` 指数退避 |
| **信号兼容** | `p4-agent` 用 `loop_start` + 事件等待而非 `loop_forever`，保证 `systemctl stop` 能跟手退出（否则必然超时） |
| **告警冷却持久化** | `monitor.py` 的冷却写 SQLite，**重启不重推**；且只在确认发出后才记冷却，断连时留给下一轮重试 |
| **重复下发去重** | `devctl.py` 对同一 id 重复指令只执行一次，回执标“重复下发，未再次执行” |
| **帧大小防线** | `p4-agent` 硬限 `MAX_FRAME_BYTES=4096`，超限不静默丢弃，而是降级发最小错误帧 |
| **会话保活 / 超时** | `p4-agent` 用 keepalive 帧 + 硬超时（`HARD_TIMEOUT_SEC`），超时回 `agent_timeout` 错误码 |
| **并发闸** | `p4-agent` 严格串行（`MAX_CONCURRENT=1`），防连点打满后端 |
| **入口隔离** | MQTT 连不上只告警，**绝不拖垮语音/业务主循环** |

**安全性**（详细配置见 [安全边界](#安全边界)）：
TLS 1.2 + `require_certificate true` 双向证书 + 账号隔离 + **按主题收窄的 ACL**。
每台设备只能看见自己的主题，**爆炸半径物理隔离**。

**通用性**（这也是本项目的边界，请读完再判断适不适合你）：

- ✅ **通用的是「怎么管」**：MQTT 主题空间 `hub/{alert,command,chat,device}/...` 分层固定，
  新增设备只需加一个 ACL 角色 + 一个适配器目录 + 一个 systemd 单元。
  小车机器人（`car_bot.py`）就是照 `p4_bot.py` 同构写出来的，**半天完成接入**，
  这就是这套分层可复用的实证。
- ⚠️ **不通用的是「怎么接」**：每台设备的报文格式、指令集、能力集必须自己定义。
  **刻意如此** —— 设备天然异构，强做代码层通用路由只会得到分支堆砌。

---

## 快速上手

### 0. 前置

- 树莓派（已在 Pi 5 / Debian 12 / Python 3.11 验证）
- `sudo apt install -y mosquitto mosquitto-clients`

### 1. 取代码 + 装依赖

```bash
git clone <本仓地址> /home/pi/cooperate/pihub
cd /home/pi/cooperate/pihub

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env，至少填：
#   WECOM_CORP_ID / WECOM_BOT_ID / WECOM_BOT_SECRET   ← 企微智能机器人（不用企微可留空）
#   smalltv_ip                                        ← 你的 ESP32 设备局域网 IP
#   http_allow                                        ← ★ 必须把你的设备 IP 加进去，否则设备取数会 403
```

🔴 **坑：`http_allow` 默认只含 `127.0.0.1`。**
照抄 `http_allow=127.0.0.1` 不改，**ESP32 一取数就是 403**，且返回体为空（设备只看状态码，
表现就是「屏幕一直空白/取不到数据」，看不出是权限问题）。

必须写成「回环 + 你的设备 IP」：

```ini
# ⚠️ 把 <你的C3的IP> 换成实际地址，例如 192.168.1.50
http_allow=127.0.0.1,<你的C3的IP>
```

> 也可以写整个网段（如 `192.168.1.0/24`），但**不推荐**——
> 那等于同网段所有设备都能读行情、控设备。**绑设备固定 IP 才是正确做法。**
> 白名单含 `0.0.0.0/0` 会直接**拒绝启动**（防手滑放通）。

> 配置优先级：**环境变量 > `.env` > SQLite `config` 表 > 代码默认值**。
> 运行期可调参数（阈值、轮询间隔等）在 DB 的 `config` 表里，
> 用 `.venv/bin/python app/config.py` 查看当前生效值。

### 3. 建库

```bash
.venv/bin/python app/init_db.py
mkdir -p logs
```

### 4. Mosquitto

```bash
# 最简：只给本机服务用（不接外部设备）
# ★ 用 localonly 这一份。直接抄 mosquitto-pihub.conf 会起不来 —— 它的 8883 段
#   写死了 password_file / acl_file / cafile / certfile / keyfile，那些文件
#   不存在时 mosquitto **直接启动失败**（不是告警）。
sudo cp config/mosquitto-localonly.conf /etc/mosquitto/conf.d/pihub.conf
sudo systemctl restart mosquitto

# 接外部设备（P4 面板 / 外部 Agent）时才需要 TLS + ACL。
# ⚠️ 下面四步是**一整套**，缺一样 mosquitto 就起不来：
sudo cp config/mosquitto-pihub.conf /etc/mosquitto/conf.d/pihub.conf
#   1) 生成 CA / 服务端证书 → /etc/mosquitto/certs/（命令见 deploy/DEPLOY.md §5）
#   2) 生成密码文件
sudo mosquitto_passwd -c /etc/mosquitto/passwd pihub
sudo mosquitto_passwd    /etc/mosquitto/passwd p4
sudo mosquitto_passwd    /etc/mosquitto/passwd agent
#   3) 装 ACL
sudo cp config/aclfile.example /etc/mosquitto/aclfile
sudo chown root:mosquitto /etc/mosquitto/aclfile && sudo chmod 640 /etc/mosquitto/aclfile
sudo systemctl restart mosquitto
```

详见 [`config/mosquitto-pihub.conf`](./config/mosquitto-pihub.conf)、
[`config/mosquitto-localonly.conf`](./config/mosquitto-localonly.conf) 与
[`config/aclfile.example`](./config/aclfile.example)。
⚠️ **`per_listener_settings true` 时认证 / ACL 必须每个 listener 各写一遍**，不继承全局——这是最容易踩的坑。

### 5. 装服务

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pihub-api pihub-devctl pihub-monitor pihub-wecom-bot
```

> 需要 ESP32-P4 控制面板时，另装 `p4-agent`，见 [`p4-agent/README.md`](./p4-agent/README.md)。

### 6. 验证

```bash
systemctl is-active pihub-api pihub-devctl pihub-monitor pihub-wecom-bot mosquitto
# 应为 5 个 active（装了 p4-agent 则再加上它，共 6 个）
# 接了 P4 面板 / 小车则另装 pihub-p4-bot / pihub-car-bot（见 systemd/）

ss -lntp | grep -E '5000|1883|8883'
curl -s localhost:5000/api/health

./acceptance_s9.sh check      # 仓库自带验收脚本
```

---

## 目录结构

```
app/            Pi 侧全部 Python 代码（api / devctl / monitor / wecom_bot / ...）
config/         Mosquitto 配置与 ACL 样例（脱敏，无真实凭据）
systemd/        6 个 systemd 单元
deploy/         ★ 部署实况记录（DEPLOY.md）—— 仓外状态、你自己该准备什么，都在这
docs/           P4 接入规格与交接文档（已脱敏）
p4-agent/       ESP32-P4 ↔ OpenClaw Agent 桥接适配器（独立服务）
acceptance_s9.sh  S9 验收脚本
.env.example    环境变量样例（值全空）
```

> ★ **`wecom_bot.py` 身兼两个身份，初读容易迷路：**
>
> 1. **可执行服务**：`pihub-wecom-bot` 跑的就是它，负责 **C3 小电视**那条链路；
> 2. **基类库**：`p4_bot.py` / `car_bot.py` 都是 `class Xxx(WeComBot)` 继承它，
>    只覆盖「凭据 / 主题 / 回显措辞 / 超时」四处。
>
> 这是刻意设计：企微 WS 长连接、鉴权、重连、环境加载这些行为**全项目只写一份**，
> 新接一台设备不需要重复实现，也不会慢慢跑偏。
> 所以看到 `from wecom_bot import WeComBot` 时，它是**被复用的库**，不是启动服务。

运行时生成、**不进仓**：`data/pihub.db`、`logs/*.log`、`backup/`、`.venv/`、**任何 `*.key` / `*.crt`**。

### 架构边界（这不是通用 MQTT 底座）

这个项目是**两种架构的结合体**，分清哪层通用、哪层不通用，才好判断适不适合你：

| 层 | 是什么 | 通用？ |
|---|---|---|
| **A. 端侧定制接入** | 每台设备写专属适配器（C3 小电视 / P4 面板 / 将来的机械臂），主题与报文各自定制 | ❌ **刻意不通用** |
| **B. Agent ↔ 设备互联** | Agent 经 MQTT / HTTP 与设备双向互通（P4 已跑通：MQTT → Gateway 流式 → 回推） | 结构可复用，接入需定制 |
| **C. 企微机器人分层管理** | 企微作统一交互入口，可接任意 Agent / 端侧设备，各建机器人 | ⚠️ **交互级通用；指令集须按设备自定** |

> **通用的是「怎么管」，不是「怎么接」。**
> 适配器长得都像「订主题 + 调能力 + 回结果」的小服务，所以体感上像通用框架，
> 但那是**形态相似**，不是抽象通用。

⚠️ **C 层的「通用」是「交互级通用」，不是「指令级通用」：**

- ✅ **交互级（通用）**：企微机器人作为一个**统一入口**，你可以接入**任何 Agent 或端侧设备**，
  并为它们建对应的机器人。告警→推送的通道（`hub/alert/#` 全通配）也现成，
  新增告警类型不用改订阅。
- ⚠️ **指令级（必须自己定）**：**每台设备的指令集得使用者按自己的端侧设备定义。**
  给小电视发“左转” / 给小车发“切换壁纸”，两边都会懵。
  本项目里 `command.py` 解析的 `set_mode` / `set_brightness` / `set_wallpaper`
  全是 C3 小电视专用；换设备必须重写解析。
- ⚠️ **推送单目标**：`get_target_chat()` 一次只推一个会话，没按类型分流。

> 一句话：**通用的是「入口与交互」，不通用的是「每台设备自己说的话」。**
> 换句话说，框架给你留好了插座，但每个插头得你自己做——毕竟一个 C3 小电视，
> 用小车的运动控制指令，这就闹笑话了。

我们**不做**代码层的多设备路由，原因：

- **设备天然异构**——C3 是 HTTP 被推，P4 是 MQTT 对话（带保活/去重/4KB 切片）。
  强套通用路由只会得到分支堆砌，代码更长却没真通用。
- **定制反而更安全**——每台设备独立进程 + 独立证书 + 独立 ACL 角色，
  一台被攻破只影响它自己，**爆炸半径物理隔离**；反之一人持所有凭据，攻破即全失。

#### 👉 想接你自己的端侧设备？

**别改 `pihub/`**（那里是行情/天气/企微的业务逻辑）。推荐：

1. **照 `p4-agent/` 抄一个适配器**——它是最小完整样本，单文件 483 行，
   含保活/去重/超时/错误码全套。
2. **更省事的做法：让你的 Agent 帮你写**。把
   [`docs/P4-PI-AGENT-SPEC.md`](./docs/P4-PI-AGENT-SPEC.md) 和 `p4-agent/` 一起喂给
   Claude Code / OpenClaw 等，说明你设备的通信方式，让它生成同构适配器。
   **这是本项目的设计假定：接入工作交给 Agent，人只负责验收。**
3. **新设备只动三处**：`config/aclfile.example` 加一段 `user <角色>`、
   `mosquitto_passwd` 加一个账号、新建 `<设备>-agent/` 目录 + 自己的 systemd 单元。

> 代价是「每接一台要写一次」，收益是**每台设备的失败都关在自己盒子里**。
> 详细论证见 [`deploy/DEPLOY.md` §9.1](./deploy/DEPLOY.md)。

---

## 安全边界

### 🌟 MQTT 主线：TLS + 双向证书 + ACL（本项目重心）

**MQTT 是设计上唯一被信任的设备通道**，安全能力全部实装，不是占位：

```ini
listener 8883 0.0.0.0
allow_anonymous false          # 必须账号密码
password_file /etc/mosquitto/passwd
acl_file      /etc/mosquitto/aclfile
cafile        /etc/mosquitto/certs/ca.crt
certfile      /etc/mosquitto/certs/broker.crt
keyfile       /etc/mosquitto/certs/broker.key
require_certificate true       # ★ 双向证书：客户端必须持合法证书，否则连接阶段就断
use_identity_as_username false # 身份走 password_file，不依赖证书 CN
```

| 能力 | 实装情况 |
|---|---|
| 传输加密 | TLS 1.2（`tls_version tlsv1.2`），broker 证书由自签 CA 签发 |
| 双向认证 | `require_certificate true` —— 无证书直接连不上 |
| 账号隔离 | `password_file`，每设备/每角色一个账号 |
| 主题级授权 | `acl_file`，**按主题收窄，绝不给 `#`**（见下） |
| 明文口隔离 | `1883` 只绑 `127.0.0.1`（`per_listener_settings true`），出不了机器 |

**ACL 粒度**（关键设计：读写分离 + 单向可见）：

```
user p4                                  # 设备侧
    topic write hub/chat/p4              #   只能发自己的话
    topic write hub/device/p4/status     #   只能报自己的状态
    topic read  hub/chat/p4/reply        #   只能读自己的回复
    topic read  hub/command/p4           #   只能收发给自己的命令
    topic write hub/command/p4/ack       #   只能回自己的回执
```

每台设备**看不到别人的任何主题**。一台设备凭据泄露，影响面锁死在它自己的主题空间内
——**爆炸半径物理隔离**，这也是为什么本项目坚持「每设备独立进程 + 独立证书 + 独立 ACL 角色」。

### ⚠️ HTTP：仅作最小闭环验证，默认只允许回环

| 端口 | 用途 | 监听范围 | 认证 |
|---|---|---|---|
| `5000` | Flask API | `0.0.0.0`（见下） | **IP 白名单**，默认仅 `127.0.0.1` |
| `1883` | MQTT 明文 | `127.0.0.1` | 匿名（仅回环，不出机器） |
| `8883` | MQTT TLS | `0.0.0.0` | 密码 + ACL + 双向证书 |

**HTTP 这条路没有做认证，请如实理解它：**

- 设计意图只是**最小闭环验证**（ESP32 直接 curl 取数据/收指令，固件侧零成本）。
- 当前实现是**全局 IP 白名单**（`http_allow`），默认只放行 `127.0.0.1`；
  白名单含 `0.0.0.0/0` 会直接**拒绝启动**，避免手滑放通。
- 白名单项支持 CIDR，但**强烈建议绑设备固定 IP 而非整网段**——
  网段放通等于同网段所有设备都能读行情、控设备。

> 💡 **要对外用，请改用 MQTT 通道。** 本项目所有「跨设备、跨网络、要长期在线」
> 的场景，都应该走 8883（TLS + 双向证书 + ACL），而不是把 5000 暴露出去。
> HTTP 只是让 ESP32 这类没有 TLS 栈的低成本 MCU 能先跑通。

🔴 **红线**：
- **不要把任何端口转发到公网。** 尤其 `5000`——白名单之外没有第二道门。
- 白名单默认只含回环；**放开前务必确认你在给谁开门**。
- `8883` 面向的是**受信任局域网**。要跨网访问，请自行加 VPN。
- 本仓**不含**任何凭据、证书、私钥、真实 IP、chat id。若你 fork 后填了真实值，**不要推回来**。

### 🗺️ 路线图：HTTP 加固（尚未实现，欢迎 PR）

HTTP 层缺认证这件事我们不打算长期留着，计划是：

1. **共享密钥头**（`X-Pihub-Key`）—— 最简单，但需固件配合发头
2. **HMAC 签名请求** —— 防重放，适合有时间的设备
3. **mTLS（等价 MQTT 8883）** —— 彻底统一，代价是 MCU 侧 TLS 开销

在那之前，**HTTP 只适合「设备与 Pi 同处受信网段 + 绑固定 IP 白名单」的场景**。

---

## 在三件套里的位置

| 件 | 仓 | 管什么 |
|---|---|---|
| **控制面** | `Agent-Ops-WeCom-Bridge` | 把企微当作 Agent 的入口：消息 ⇄ Claude Code / OpenClaw |
| **数据面（本仓）** | `Agent-Ops-Edge-Hub` | 把 MQTT 当作设备的入口：指令 ⇄ 端侧设备 |
| 总蓝图 | `Agent-Ops-Architecture` | 只放架构文档与人机协同基准 |

**分工一句话**：控制面管「人怎么指挥 Agent」，数据面管「Agent 怎么指挥设备」。
两个仓合起来，才是完整的「人 ⇄ Agent ⇄ 物理世界」这条路。

> 📌 本仓**自带**企微接入教程（`ai-agent-wecom/`）与 P4 接入契约（`docs/`），可以直接照着接设备。
> **不含** ESP32 固件、硬件资料与外壳素材 —— 那些属于设备本身。
>
> 早先还有一个**只服务 C3 小电视**的定制版本。它与本仓**不是包含关系**：
> 那是单设备定制，本仓是**可接多设备**的底座。
