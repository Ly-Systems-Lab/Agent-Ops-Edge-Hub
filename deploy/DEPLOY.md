# DEPLOY · 树莓派侧部署实况（已脱敏）

> 目的：把**不在 git 仓里**、但复现离不开的东西记下来。
> 填写人：树莓派侧 Agent · 填写日期：`2026-10-26`
> 🔴 本文只含事实与结构；**不含任何真实 token / 私钥 / chat id / 用户 id / 设备 MAC / 内网 IP**。
>    值一律用 `<REDACTED>` 或 `<EXAMPLE>`。

---

## 1. 这台机器

| 项 | 值 |
|---|---|
| 主机名 / 角色 | `raspberrypi` ／ Pi Hub 中控 |
| OS | Debian GNU/Linux 12 (bookworm) |
| 内核 | `Linux raspberrypi 6.12.21-v8-16k+ #1870 SMP PREEMPT Wed Apr 2 20:19:34 BST 2025 aarch64` |
| Python | `3.11.2`（venv 内同版本） |
| Mosquitto | `mosquitto version 2.0.11` |
| systemd | `252 (252.39-1~deb12u1)` |
| 部署目录 | `/home/pi/cooperate/pihub` |
| 运行用户 | `pi`（组 `pi`） |
| 硬件 | 树莓派 5（aarch64，16K 页内核） |

**采集命令**：

```bash
uname -a
grep PRETTY_NAME /etc/os-release
python3 -V
mosquitto -h | head -2
systemctl --version | head -1
```

---

## 2. Python 依赖

`requirements.txt`（直接依赖，与主仓一致）：

```text
Flask==3.1.3
paho-mqtt==2.1.0
requests==2.34.2
websocket-client==1.9.2
```

实际安装版本（`<venv>/bin/pip freeze`，含间接依赖）：

```text
blinker==1.9.0
certifi==2026.7.22
charset-normalizer==3.5.1
click==8.5.0
Flask==3.1.3
idna==3.19
itsdangerous==2.2.0
Jinja2==3.1.6
MarkupSafe==3.0.3
paho-mqtt==2.1.0
requests==2.34.2
urllib3==2.7.0
websocket-client==1.9.2
Werkzeug==3.1.8
```

> `websocket-client` 是企微智能机器人**长连接模式**必需的（不是可选依赖）。

---

## 3. 目录布局

```text
/home/pi/cooperate/pihub/
├── app/                 # 14 个 .py + schema.sql（仓内）
├── systemd/             # 6 个 unit（仓内）
├── config/              # Mosquitto 配置 + ACL 样例（仓内，脱敏）
├── deploy/              # 本文件（仓内）
├── acceptance_s9.sh     # 验收脚本（仓内）
├── .env.example         # 样例（仓内，值全空）
├── .env                 # 🔴 真实凭据（仓外，见 §7）
├── data/pihub.db        # 🔴 运行时数据库（仓外）
├── logs/*.log           # 🔴 运行日志（仓外）
└── .venv/               # 🔴 虚拟环境（仓外）
```

**仓内**：`app/ systemd/ config/ deploy/ acceptance_s9.sh requirements.txt README.md LICENSE .env.example`
**运行时生成（都不进仓）**：`data/ logs/ backup/ .venv/ __pycache__/`

⚠️ 部署目录本身**不是 git 仓**（代码在 git 里维护，部署目录是另外一份）。
改代码时注意**别在两处各改一份** —— 否则下次同步会互相覆盖。

---

## 4. systemd 实际单元（7 个）

`pihub/` 内 **6 个**单元结构一致，仅 `Description` / `ExecStart` / 日志文件不同。共同点：

- `User=pi` / `Group=pi`
- `Environment=PYTHONUNBUFFERED=1`
- `Environment=PIHUB_DB=<部署目录>/data/pihub.db`
- `Restart=always` / `RestartSec=5`
- `StandardOutput`+`StandardError` → `append:<部署目录>/logs/<name>.log`

```ini
# pihub-api.service
After=network-online.target mosquitto.service
ExecStart=<部署目录>/.venv/bin/python <部署目录>/app/api.py
日志=logs/api.log

# pihub-devctl.service
After=network-online.target mosquitto.service pihub-api.service
ExecStart=<部署目录>/.venv/bin/python <部署目录>/app/devctl.py
日志=logs/devctl.log

# pihub-monitor.service
After=network-online.target mosquitto.service
ExecStart=<部署目录>/.venv/bin/python <部署目录>/app/monitor.py
日志=logs/monitor.log

# pihub-wecom-bot.service
After=network-online.target mosquitto.service
ExecStart=<部署目录>/.venv/bin/python <部署目录>/app/wecom_bot.py
日志=logs/wecom_bot.log

# pihub-p4-bot.service          （2026-10-28 接入）
After=network-online.target mosquitto.service
ExecStart=<部署目录>/.venv/bin/python <部署目录>/app/p4_bot.py
日志=logs/p4_bot.log

# pihub-car-bot.service         （2026-10-30 接入）
After=network-online.target mosquitto.service
ExecStart=<部署目录>/.venv/bin/python <部署目录>/app/car_bot.py
日志=logs/car_bot.log
```

> 单元里**没有** `Environment=` 携带任何密钥——所有凭据走 `.env` 文件。这是刻意设计，便于脱敏进仓。

**三个设备机器人各用一套独立凭据**（互不复用，各自建一个企微机器人）：

| 服务 | 脚本 | `.env` 键 | MQTT 主题 |
|---|---|---|---|
| `pihub-wecom-bot` | `wecom_bot.py` | `WECOM_BOT_ID` / `_SECRET` | 发 `hub/command/smalltv`，收 `hub/alert/#` |
| `pihub-p4-bot` | `p4_bot.py` | `P4_WECOM_BOT_ID` / `_SECRET` | 发 `hub/command/p4`，收 `hub/command/p4/ack` |
| `pihub-car-bot` | `car_bot.py` | `CAR_WECOM_BOT_ID` / `_SECRET` | 发 `hub/command/car`，收 `hub/command/car/ack` |

> `p4_bot.py` / `car_bot.py` 都是 `class Xxx(WeComBot)` 继承 `wecom_bot.py`，**基类一行不改**——
> 只覆盖「凭据来源 + 指令解析 + 主题 + 回执配对」。这样心跳线程泄漏、退避不重置、
> pending 无界增长那些修过的坑不会在第二份实现里重犯。

### 另一个单元：`p4-agent`（独立目录）

不在 `pihub/` 内，部署在 `/home/pi/cooperate/p4-agent`。它**用 `EnvironmentFile=` 读 `.env`**，
这是与上面 6 个的区别（上面 6 个靠脚本自己解析 `.env`）。

```ini
# p4-agent.service
WorkingDirectory=/home/pi/cooperate/p4-agent
EnvironmentFile=/home/pi/cooperate/p4-agent/.env
ExecStart=/usr/bin/python3 /home/pi/cooperate/p4-agent/app/p4_agent.py
日志=logs/agent.log
```

**开机自启状态**（7 个全部 enabled）：

```bash
systemctl is-enabled pihub-api pihub-devctl pihub-monitor pihub-wecom-bot
systemctl is-enabled pihub-p4-bot pihub-car-bot
systemctl is-enabled p4-agent
# enabled × 7
```

⚠️ **换机器部署时必须改路径**：单元里 `WorkingDirectory` / `ExecStart` / `PIHUB_DB` /
`StandardOutput` 都是**绝对路径**，写死为 `/home/pi/cooperate/pihub`。部署到别的目录要全局替换。

---

## 5. Mosquitto（broker）

**安装位置**：`/etc/mosquitto/conf.d/pihub.conf`（主配置 `/etc/mosquitto/mosquitto.conf` 只保留 `include_dir`）

| 项 | 值 |
|---|---|
| 监听地址 / 端口 | `127.0.0.1:1883`（明文）+ `0.0.0.0:8883`（TLS） |
| `per_listener_settings` | `true` ← 关键，认证不继承 |
| 1883 是否允许匿名 | `yes`（仅因为绑死回环，不出机器） |
| 8883 认证方式 | `password_file` + `acl_file` + **双向证书（mTLS）** |
| `password_file` 路径 | `/etc/mosquitto/passwd`（`root:mosquitto 640`，内容不贴） |
| `acl_file` 路径 | `/etc/mosquitto/aclfile`（`root:mosquitto 640`，骨架见仓内 `config/aclfile.example`） |
| TLS | 开启；端口 `8883`；`require_certificate true`；`use_identity_as_username false`；`tls_version tlsv1.2` |
| CA / 服务端证书 | `/etc/mosquitto/certs/ca.crt`、`broker.crt`（私钥 `broker.key` **不提交、不外传**） |
| 证书有效期 | `notAfter=Oct 20 17:21:12 2036 GMT`（CA 与服务端证书同期，10 年） |

**ACL 角色**（5 个，主题按前缀收窄，绝不给外部角色 `#`）：

| 角色 | 权限 |
|---|---|
| `pihub` | `readwrite hub/alert/#`、`hub/command/#`、`hub/chat/#`（本机 6 个服务共用；**仅本机回环**） |
| `p4` | `write hub/chat/p4`、`hub/device/p4/status`、**`hub/command/p4/ack`**；`read hub/chat/p4/reply`、**`hub/command/p4`** |
| `agent` | `read hub/chat/p4`、`hub/device/p4/status`、**`hub/command/p4/ack`**；`write hub/chat/p4/reply`、**`hub/command/p4`** |
| `car` | `read hub/command/car`；`write hub/command/car/ack`（2026-10-30 新增） |
| `observer` | `read hub/#`（仅本机排障，**不要给外部设备**） |

> ⚠️ **`p4` 的反控两行不加，P4 就收不到命令**：MQTT 3.1.1 下订阅一个被 ACL 拒的主题，
> broker 会拒绝该订阅（mosquitto 默认还会**直接断连**）⇒ 现象是"反复重连、没有明确报错"。

> **演进史**：早期只有 `listener 1883 127.0.0.1` + 匿名。
> 2026-10-24 为接 ESP32-P4 控制面板改为**双监听**（1883 保持原样零改动，另开 8883 TLS）。
> 这套设计的好处：**旧链路一行没动**，新设备走新端口，出问题互不牵连。

**常用命令**：

```bash
sudo systemctl restart mosquitto
sudo mosquitto_sub -h 127.0.0.1 -p 1883 -t '#' -v            # 本机明文自检
sudo tail -f /var/log/mosquitto/mosquitto.log
```

---

## 6. 端口与防火墙

```bash
ss -lntp | grep -E '5000|1883|8883'
```

| 端口 | 用途 | 监听范围 | 对外？ |
|---|---|---|---|
| `5000` | Flask API | `0.0.0.0` | ✅ 局域网可访问（**IP 白名单**，默认仅 `127.0.0.1`；白名单外 403） |
| `1883` | MQTT 明文 | `127.0.0.1` | ❌ 不出机器 |
| `8883` | MQTT TLS | `0.0.0.0` | ✅ 局域网可访问（密码+ACL+mTLS） |

**防火墙**：`ufw` **未安装 / 未启用**，`nftables` 无自定义规则。

> 🔴 安全边界靠**服务自身绑定**（1883 绑死回环）+ **HTTP 白名单**而非防火墙。
> 若这台机器会被带到不受信任的网络，**建议先启用 ufw**：
> ```bash
> sudo apt install -y ufw
> sudo ufw allow 22/tcp
> sudo ufw allow 5000/tcp
> sudo ufw allow 8883/tcp
> sudo ufw enable
> ```
> 🔴 **绝不能**把任何端口转发到公网——`5000` 的 IP 白名单是**唯一**那道门
> （没有 token、没有签名），暴露即等于把设备控制权交给能伪造源 IP 之外的一切人。

### 6.1 HTTP 白名单（2026-10-30 新增）
`api.py` 加了全局 `before_request` 守卫：白名单外一律 **403**，函数体不执行。
白名单来自配置项 `http_allow`，默认 `127.0.0.1`；支持 CIDR；**含 `0.0.0.0/0` 直接拒绝启动**。

⚠️ **默认值会让 ESP32 取不到数**（403，而且返回体为空 ⇒ 屏幕上表现为"一直空白"，
看不出是权限问题）。**部署时必须把设备 IP 加进去**：`http_allow=127.0.0.1,<设备IP>`。

### 6.2 被动学习 C3 的 IP
同一版 `api.py` 还补上了原来缺的那半个闭环：C3 只要来取数（`/api/stock*`、`/api/weather`），
Pi 就把它的真实源 IP 记进 `smalltv_ip`。四道边界：只认 **GET**、只认 **5000** 端口、
拒绝回环、**只从白名单内学习**；同一 IP **30 s** 内只写一次库。

⇒ 手工填 `smalltv_ip` 从"必须"降为"推荐"（省掉等它先来取数那一段）。

---

## 7. 配置键（只有键名，没有值）

`.env` 实际键（截至 2026-10-30，共 3 组企微凭据 + 1 个白名单，全部已填真值）：

```dotenv
WECOM_CORP_ID=<REDACTED>
WECOM_BOT_ID=<REDACTED>            # ① 小电视（C3）
WECOM_BOT_SECRET=<REDACTED>
# ② P4 面板 —— 若已接入
#P4_WECOM_BOT_ID=<REDACTED>
#P4_WECOM_BOT_SECRET=<REDACTED>
# ③ 曼波小车 —— 若已接入
#CAR_WECOM_BOT_ID=<REDACTED>
#CAR_WECOM_BOT_SECRET=<REDACTED>
http_allow=<REDACTED>              # 形如 127.0.0.1,<设备IP>
```

> ⚠️ **三对凭据互相独立**，各建一个企微机器人，不要复用——复用会让"哪个机器人收到这条消息"
> 变得不可预测，而且一个泄露就连带三台设备。

> 注意：`smalltv_ip` **不在 `.env` 里**，而是存在 SQLite `config` 表（见下）。

SQLite `config` 表实际键（23 个）：

```text
alert_cooldown_sec          # 告警冷却秒数
kline_fetch_at:sh000001     # 各 symbol 的日K最后拉取时间（运行时）
kline_fetch_at:sz000725
kline_fetch_at:sz002436
kline_fetch_at:sz002841
kline_ttl                   # 日K缓存 TTL
last_brightness             # 小电视最后亮度（运行时）
limit_down_pct              # 主板跌停阈值
limit_down_pct_gem          # 创业板跌停阈值
limit_up_pct                # 主板涨停阈值
limit_up_pct_gem            # 创业板涨停阈值
pihub_host                  # = 127.0.0.1（服务端已绑 0.0.0.0，免疫换 IP）
pihub_port                  # = 5000
poll_interval_idle          # 非交易时段轮询间隔
poll_interval_trading       # 交易时段轮询间隔
smalltv_ip                  # 🔴 <REDACTED> ESP32 小电视局域网 IP
surge_threshold             # 异动阈值
upstream_fail_threshold     # 上游连续失败阈值
weather_cache_sec           # 天气缓存 TTL
wecom_bot_id                # 🔴 <REDACTED>
wecom_chat_id               # 🔴 <REDACTED> ① C3 小电视机器人的推送目标会话
wecom_chat_type             # = 2（1 单聊 / 2 群聊）
wecom_chat_id_p4            # ② P4 面板机器人自己的推送目标（★ 与上面分键，勿合并）
wecom_chat_type_p4          # = 2
wecom_chat_id_car           # ③ 曼波小车机器人自己的推送目标
wecom_chat_type_car         # = 2
wecom_webhook_key           # 空（本部署走长连接模式，不用 webhook）
```

> ⚠️ **`wecom_chat_id*` 为什么按机器人分三份**：三个 bot 是三个进程但**共用一个库**，
> 早先都写同一个 `wecom_chat_id` ⇒ 谁最后收到消息，谁就改掉了**告警**机器人的推送目标。
> 三个机器人同群时看不出来（chatid 相同），一旦分群，警报会被发到别的会话、企微拒收、
> **静默丢失**（`send_markdown` 的返回值只看 websocket 发出没有，不看服务端 errcode）。
> ⇒ 现在每个 bot 用自己的一组键；**别再把它们合并**。

> ⚠️ **配置来源有三处**（这是换机器最容易漏的地方）：
>
> | 来源 | 内容 |
> |---|---|
> | `/home/pi/cooperate/pihub/.env` | 3 个企微凭据 |
> | `pihub/data/pihub.db` 的 `config` 表 | 23 个键（阈值、轮询间隔、`smalltv_ip` …）+ 本次新增的 4 个按机器人分的推送目标键（各 bot 首次收到消息时才落库） |
> | `/home/pi/cooperate/p4-agent/.env` | 12 个 P4 键（`P4_MQTT_PASS` / `P4_GATEWAY_TOKEN` 必填） |
>
> 换机器时：`pihub/.env` 要重建，`config` 表要在 `init_db.py` 之后**重新录入**
> （`smalltv_ip` 必填，否则"发指令"必然失败，表现为企微回 `执行失败（ESP32 返回 502）`），
> `p4-agent/.env` 另建（样例见 `p4-agent/.env.example`）。

查看 pihub 当前生效值：`<venv>/bin/python app/config.py`

### 7.1 `p4-agent/.env` 键名（12 个，值全脱敏）

```dotenv
P4_DEVICE_ID=p4                  # 决定主题 hub/chat/<id>
P4_TOPIC_PREFIX=hub
P4_MQTT_HOST=<REDACTED>          # 实机填的内网 IP；仓内代码默认值已改为 127.0.0.1
P4_MQTT_PORT=8883
P4_MQTT_USER=agent               # ACL 角色名
P4_MQTT_PASS=***
P4_CA_FILE=<REDACTED 或空>
P4_AGENT_CERT=<REDACTED 或空>
P4_AGENT_KEY=<REDACTED 或空>
P4_GATEWAY_URL=<REDACTED>
P4_GATEWAY_MODEL=openclaw/main
P4_GATEWAY_TOKEN=***
```

> `P4_CA_FILE` / `P4_AGENT_CERT` / `P4_AGENT_KEY` 在实机 `.env` 里显式写了绝对路径；
> 仓内代码已改为**默认相对解析**到 `<repo>/p4-agent/certs/`，所以新部署可以不填这三项。

---

## 8. DB schema（只有结构，没有数据）

完整 schema 见仓内 [`app/schema.sql`](../app/schema.sql)。实际库中含 7 张表：

| 表 | 用途 | 是否含用户数据 |
|---|---|---|
| `symbols` | 自选股列表 | 🔴 是（本机 3 条） |
| `quote_cache` | 实时行情缓存（每 symbol 一行） | 否（可重建，7 条） |
| `kline_history` | 日K历史 | 否（可重建，237 条） |
| `weather_cache` | 天气缓存 | 否（可重建，4 条） |
| `alert_cooldown` | 告警去重冷却 | 否（0 条） |
| `config` | 可调参数（见 §7） | 🔴 是（含 chat id / 设备 IP） |
| `wecom_chat` | 企微会话（长连接推送目标） | 🔴 是（每台设备 1 条 chat id；三个机器人同群时是同一个值） |

> 🔴 导出/迁移时**只导 schema**，不要导 `symbols` / `wecom_chat` / `config` 的数据行。

---

## 9. 设备清单（脱敏）

| device id | 类型 | 传输 | 状态 | 代码位置 |
|---|---|---|---|---|
| `smalltv` | ESP32-C3 小电视 | HTTP（Pi → 设备 `/set`）+ `hub/command/smalltv` | ✅ **在线跑着** | `pihub/app/{devctl,command,api}.py` |
| `p4` | ESP32-P4 控制面板 | MQTT 双向（8883 TLS）；反控走 `hub/command/p4` | ✅ **已打通，实测可对话 + 可反控** | `p4-agent/app/p4_agent.py` + `app/p4_bot.py` |
| `car` | 曼波语音小车 | MQTT `hub/command/car` | ✅ **已接入（2026-10-30）** | `app/car_bot.py` |
| `cc` | Windows 侧 Claude Code | MQTT（8883 TLS，角色 `agent`） | 计划中 | — |

**三台设备 = 三个企微机器人 = 三套独立凭据**，互不复用：

| 机器人 | 入口 | 下发的主题 | 回执 |
|---|---|---|---|
| 小电视 | `pihub-wecom-bot` | `hub/command/smalltv` | `hub/command/smalltv/ack` |
| P4 面板 | `pihub-p4-bot` | `hub/command/p4` | `hub/command/p4/ack` |
| 曼波小车 | `pihub-car-bot` | `hub/command/car` | `hub/command/car/ack` |

> ★ **三者应答模型不同，是有意为之**：小电视/P4 是"同步等回执"（P4 超时 5 s）；
> 小车动作长（「露一手」≈ 转 3 圈 + 21 s 音乐），所以**收到即回「已受理」**，
> 动作结束再由车侧补一条 `done` 走异步通知。照抄时别把这条差异抹平。

> 设备 MAC / 局域网 IP 一律 `<REDACTED>`；只保留"有几台、什么类型、走什么协议"。

### 9.1 ★ 架构定位：这是「两种架构的结合体」，不是通用 MQTT 底座

**结论先说**：本项目**不做**代码层的多设备路由，也不追求 MQTT 层的通用性。
它的可复用价值在**分层管理架构**这一层，而不在「一套代码适配所有设备」。

#### 结合体的两半

| 架构 | 具体是什么 | 通用性 |
|---|---|---|
| **A. 定制端侧接入** | 每台端侧设备（C3 小电视 / ESP32-P4 面板 / 将来的机械臂…）写**专属适配器**，主题与报文契约各自定制 | ❌ **刻意不通用**——按设备定制 |
| **B. Agent ↔ 设备互联** | Agent 经 MQTT / HTTP 与设备双向互通（P4 已跑通：MQTT → Gateway 流式 → 回推） | 结构可复用，接入需定制 |
| **C. 企微机器人分层管理** | 企微作统一交互入口，可接任意 Agent / 端侧设备，各建机器人 | ⚠️ **交互级通用；指令集须按设备自定** |

> 换句话说：**通用的是「怎么管」，不是「怎么接」**。
> 由于 A 层的适配器长得都像「一个订主题、一个调 API 的小服务」，
> 体感上确实像通用框架——但那是**形态相似**，不是抽象通用。

#### 为什么刻意不做 MULTI-DEVICE 路由

1. **端侧设备天然异构**。C3 是 HTTP 被推（Pi 主动 POST `/set`）；P4 是 MQTT 对话
   （设备发起、流式回复、带保活/去重/4KB 切片）。硬套一层通用路由，得到的是
   `if kind == 'http' ... elif kind == 'mqtt' ...` 的分支堆砌，**代码更长、更难读，却没有真通用**。
2. **定制接入反而更安全**。每台设备一个独立进程 + 独立证书 + 独立 ACL 角色：
   任一设备被攻破，只影响它自己的主题与凭据，**爆炸半径被物理隔离**。
   反之一个「万能路由」进程持有所有设备的凭据，攻破即全失。
3. **契约是设备专属的**。P4 的五条硬约束（4KB / id 回传 / final 恰好一次 / 20s 保活 / 不 retained）
   是为那块 LVGL 屏定的；换一块屏就得重定。把这些塞进「通用层」会让通用层被单一设备的怪癖污染。

#### 代码层的实际情况（证据）

`pihub/` 的设备指令路径是**一台设备一个常量**：

```python
# app/devctl.py:36-37
CMD_TOPIC = "hub/command/smalltv"        # 写死 smalltv
ACK_TOPIC = "hub/command/smalltv/ack"

# app/wecom_bot.py:83-84
CMD_TOPIC = "hub/command/smalltv"        # 又一份，两份各自维护

# app/p4_bot.py:40-41     ← 2026-10-28
CMD_TOPIC = "hub/command/p4"
CMD_ACK_TOPIC = "hub/command/p4/ack"

# app/car_bot.py:40-41    ← 2026-10-30，照 p4_bot.py 同构
CMD_TOPIC = "hub/command/car"
CMD_ACK_TOPIC = "hub/command/car/ack"
```

设备地址也是单值：`ip = cfg("smalltv_ip", "")`（不是 `dev_id -> addr` 的映射）。

`p4-agent/` 稍好一点：`P4_DEVICE_ID` 可配、主题 `f"{prefix}/chat/{DEVICE_ID}"` 按设备拼接，
但一个进程只订阅一个 topic——**接第二台得再起一个 systemd 实例**。

⇒ **这是设计选择，不是待修的短板。**

📌 **但"复制一份改三处"是有代价的，得说清楚**：
`p4_bot.py`、`car_bot.py` 都继承 `wecom_bot.py`，**只覆盖四处**（凭据来源 / 指令表 / 主题 / 回执配对），
所以基类里修过的坑（心跳线程泄漏、退避重置、pending 无界增长）不会重犯——**这是继承带来的，不是复制带来的**。
真正复制的是那一整套「peek 循环 + 超时扫描 + 回执配对」的骨架：
三个文件里各有一份，将来改一处**得记得改三处**。
目前接受这个代价（三个文件共 ≈ 675 行，读起来直白）；**若接第四台，再考虑抽基类**，
而不是现在就为一个还没出现的需求做抽象。

#### ★ “C 层通用”的准确含义：**交互级通用，非指令级通用**

用户的原话更准：“企微通用就是入口这块——这是**交互级通用**”。别过度期待：

| | 通用性 | 说明 |
|---|---|---|
| **接入入口** | ✅ **通用** | 企微机器人做统一交互入口，你可以接入**任何 Agent 或端侧设备**，各自建机器人 |
| **告警外发** | ✅ **通用** | `hub/alert/#` 全通配，新增告警类型不用改订阅 |
| **推送目标** | ⚠️ 半通用 | `get_target_chat()` 支持多会话，但一次只推一个，**无按类型分流** |
| **指令集** | ❌ **必须自己定** | 每台设备的指令语义不同。`command.py` 的 `set_mode`/`set_brightness`/`set_wallpaper` 全是 C3 小电视专用 |
| **指令路由** | ❌ **单设备** | `CMD_TOPIC = "hub/command/smalltv"` 写死 |

> **框架给你留好了插座，但每个插头得你自己做。**
> 一个 C3 小电视，用小车的运动控制指令，这就闹笑话了。
> ⇒ 接入新设备时，**连指令集都是你定制的一部分**，不是拿来就用的。

#### ⚠️ 给使用者的建议：接入你自己的端侧设备

**不推荐**你改 `pihub/` 去适配新设备（那里是行情/天气/企微的业务逻辑，动它风险高）。
推荐路径：

1. **照着 `p4-agent/` 抄一个适配器**。它是最小完整样本：订主题 → 调能力 → 回结果，
   单文件 483 行，含保活/去重/超时/错误码全套。
2. **让 Agent 自己帮你写**。把 `docs/P4-PI-AGENT-SPEC.md` + `p4-agent/` 同时喂给你的 Agent
   （Claude Code / OpenClaw 等），说明你的设备通信方式，让它生成一个同构适配器。
   这是本项目的设计假定：**接入工作交给 Agent，人只负责验收**。
3. **新设备只动这三处**，不碰核心：
   - `config/aclfile.example` 加一段 `user <新设备角色>` + 最窄主题
   - `mosquitto_passwd` 加一个账号
   - 新的 `<设备>-agent/` 目录 + 自己的 systemd 单元
4. **保持隔离**：一台设备一套证书、一个 ACL 角色、一个进程。

> 这样做的代价是「每接一台要写一次」，收益是**每台设备的失败都关在自己的盒子里**。
> 对受信任局域网内的家用/桌面中控场景，这个交换是划算的。

## 9.2 已知限制与坑（代码核出，按严重度排序）

### 🔴 已修：P4 对话上下文丢失

**问题**：`p4_agent.py` 早期用 `conv = f"conv:p4-{msg_id}"` 当会话 ID。
`msg_id` 每句话都不同 ⇒ **每句一个全新会话**，P4 上无法多轮对话
（说"把它改成 30"这种指代完全失效）。

**实测证据**：

```
同 conv 连问两轮：第1轮"记住蓝鲸" -> 第2轮问"刚才的词" -> 答"蓝鲸"   ✅
换新 conv 问同一问题               -> 答"No response"              ❌
```

**已改为**：`conv = f"conv:{DEVICE_ID}-main"`（固定会话，符合 SPEC §9"只有一条对话流"）。
**副作用**：历史持续累积，需清空则重启 Gateway 或改 `DEVICE_ID`。

### 🟡 已修：并发（从无上限 → 2 → **严格串行 1**）

`on_message` 每条消息起一个线程，无限制。连发 N 句 = N 个线程同时打 Gateway，
每个还附带一个 20s 保活线程。先加 `MAX_CONCURRENT=2`，**实测后改为 1**。

#### ★ 为什么最终是 1：一个被实测推翻的假设

早期注释写的是「P4 物理上是一次一句（气泡期间不发下一条）」。**这个假设是错的。**

实测日志（用户连发两句）：

```
[00:04:15] [recv] id=p4-...-0003 text='帮我看看淄博近7天的天气吧'
[00:04:21] [recv] id=p4-...-0004 text='你哈'        ← 上一句还没 done 就进来了
[00:04:28] [done] id=p4-...-0003 frames=543
[00:04:30] [done] id=p4-...-0004 frames=17
```

⇒ **P4 不会等上一句回完再发下一句。** 两个线程真的在并行跑，
但 `-0004` 只有 17 帧，而 `-0003` 有 543 帧在流式输出 ——
小回复被大回复的流"压"到最后才显完，**用户看到的就是"排队"**。

> 所以「排队」不是 P4 固件的设计，是**我们这边两个线程抢同一个气泡**造成的。

**改为 `MAX_CONCURRENT=1`**：第二条立即回 `agent_busy`（SPEC §5.3 允许的四个错误码之一），
语义明确。**刻意不做 FIFO 队列** —— SPEC §5.4 第 3 条明确写「你这边也不需要为它保留待投递队列」。

**一个容易忽略的细节**：拒绝发生在 `dedup.check_and_mark()` **之前**，
所以被拒的 id **没有**被标记为已处理 ⇒ **用户重发同一句话仍会正常执行**，
不会被当成重复而静默忽略。这点已写进代码注释。

### 🟢 已修：帧超限曾静默丢弃

**原问题**：`_publish()` 遇到超 4KB 的帧只打一行 `[WARN] 帧超限 ... 强制截断`
就 `return False`，**调用方不处理**。P4 侧表现为气泡一直卡到 60s 超时，
用户完全看不出发生了什么。（且那句"强制截断"文案与实际行为——直接丢弃——不符。）

**触发条件**（实测）：`split_deltas()` 的 `budget = SAFE - base_overhead`
在 `id` 极长（> 14000 字符）时被压成负数 → clamp 到 200 → 切出的帧远超 4KB。
正常 `id`（18 字）最坏帧 3496B < 3500B SAFE，**实际不触发**。

**已修**：拆成 `_raw_send()`（纯发送）+ `_publish_final_error()`（终结错误帧，带尺寸自愈）：

```
_publish() 失败 → _publish_final_error("internal", "回复内容过长，已中止")
                    ↓ 逐级裁短 msg 重试
                  [原文] → ["内部错误（报文超限）"] → ["internal"] → [""]
                    ↓ 都不行
                  [FATAL] 记日志（此时 id 本身超 4KB，P4 也配不上对）
```

细节：
- `publish()` 抛异常 = 链路断了，标 `offline` 后**不再做无意义的裁短重试**
- `error()` 走同一条自愈路径
- `final()` 发前检查降级后是否已 `closed`，**避免重复 final**
- 新增 `_tag()`：日志里 id 超 48 字裁短，避免刷屏

**回归 7 项全过**：正常 / `final` 恰好一次 / 超长降级 / 降级后不重复 /
链路异常 / 极长 id / **长文本正常切片不误触发降级**（最关键，防误伤正常长回复）。

### 🔴 已修：`systemctl stop/restart` 必然超时

**问题**：`main()` 里只有 `except KeyboardInterrupt`，**没有任何 SIGTERM 处理器**。
而 systemd 的 stop/restart 发的是 SIGTERM，paho 的 `loop_forever()` 不会自己响应它
⇒ `systemctl stop` 一直卡到 `TimeoutStopSec` 超时，最后被 SIGKILL。

**实测**：

```
systemctl stop p4-agent      -> Failed to stop: 连接超时
systemctl restart p4-agent   -> Failed to restart: 连接超时
systemctl kill -s SIGKILL    -> Failed to kill: 连接超时   （signals 也走同一路径）
kill -9 <pid>                -> 立即生效
```

⇒ 运维表现：**改了代码重启不生效**（服务看起来 active，但进程还是旧的）。
本次修 P4 上下文 bug 时就撞上了这个坑，必须先 `kill -9` 再 start。

**已修**：注册 SIGTERM/SIGINT → 置 `shutdown_evt` 事件；
主循环由 `loop_forever()` 改为 `loop_start()` + `shutdown_evt.wait(1.0)`，
这样能被信号唤醒，`finally` 里 `disconnect()` + `loop_stop()`。

**验证**：`kill -TERM <pid>` → 日志输出 `[main] 收到信号 15，准备退出` / `[main] 退出`，
进程在 1 秒内优雅退出，随后 `Restart=always` 自动拉起。

### 🟡 未修：DESIGN 层 —— P4 无用户鉴权

`hub/chat/p4` 靠 mTLS + ACL 隔离（P4 只能写自己的主题）。这是"设备级信任"，
不是"用户级鉴权"—— **拿到 P4 设备的人就能对你的 Agent 说任何话**
（包括让它改文件、执行命令）。

> 对家庭内部设备可接受，但如果 P4 会被带到外面，需评估。

### 🔵 设计选择（不是缺陷）

- **每设备一个适配器**：见 §9.1
- **`pihub/` 指令路径单设备硬编码**：见 §9.1
- **`check_hostname=False` + `tls_insecure_set(True)`**：因为内网 IP 会变、
  证书 SAN 可能不含新 IP。靠 CA 校验链 + 双向证书保证，放弃主机名校验。
  在家庭内网可接受，**不建议用于公网**。

---

## 10. 相对「裸装的基线」改了什么

### ⚠️ 先说重要结论

**`app/` 下这 10 个文件与本仓的初始基线逐字节完全一致**（已用 `diff` 全部核对）。

```
app/api.py        SAME
app/cache.py      SAME
app/command.py    SAME
app/config.py     SAME
app/datasource.py SAME
app/devctl.py     SAME
app/init_db.py    SAME
app/monitor.py    SAME
app/schema.sql    SAME
app/wecom_bot.py  SAME
acceptance_s9.sh  SAME
```

⇒ **Pi 侧的真实改动全在「仓外」**（Mosquitto 配置、证书、密码、ACL、DB 数据、`.env`）。
这正是本仓需要 `deploy/DEPLOY.md` 的原因。

> 基线一致性：因为部署目录不是 git 仓，**无法用本地历史证明**"当前部署 == 基线"。
> 上述逐文件 diff 是间接证据（全部 SAME），可信度较高。

### 代码层

**2026-10-30 复核：** 上面那张"全部 SAME"只对 10-27 之前成立。之后 `app/` 多了 4 个文件：
`p4_bot.py` / `p4_command.py`（P4 设备机器人，10-28）、`car_bot.py` / `car_command.py`（小车，10-30）；
`api.py` 也多了 HTTP 白名单与被动学 IP（见 §6 端口、§7 配置键）。

### 部署层（不在 git 里的改动）

```text
- Mosquitto：基线只有 listener 1883 127.0.0.1 + allow_anonymous
             → 2026-10-24 改为双监听：
                 · 1883 127.0.0.1 明文匿名（旧链路零改动）
                 · 8883 0.0.0.0 TLS + password_file + acl_file + mTLS
             → 新增 /etc/mosquitto/{passwd,aclfile,certs/{ca.crt,broker.crt,broker.key}}
             → per_listener_settings true（认证每 listener 各写一遍）
- systemd：`pihub/` 原 4 个单元与基线一致（基线仓内已有 4 个 .service）
           → 2026-10-28 加 pihub-p4-bot.service
           → 2026-10-30 加 pihub-car-bot.service   ⇒ 现共 6 个
           实际部署在 /etc/systemd/system/，6 个全部 enabled
- ★ 另有第 7 个服务 p4-agent（不在基线的任何版本里）
           位置 /home/pi/cooperate/p4-agent，独立目录，enabled + active
           它把 ESP32-P4 接入 OpenClaw Agent，已实测可对话（日志有真实收发记录）
- 防火墙：无（ufw 未安装）
- Python：仅 requirements.txt 所列 + 间接依赖；websocket-client 为企微长连接必需
- 其他：
  · .env 含 3 组企微凭据（小电视 / P4 面板 / 曼波小车，仓外）
  · SQLite config 表 23 个键，其中 smalltv_ip / wecom_bot_id / wecom_chat_id 为真值
  · 证书 10 年有效期（至 2036-10-20）
```

### 本分支额外补充的文件（相对 main）

```text
deploy/DEPLOY.md              新增（本文件）
config/mosquitto-pihub.conf   修改（基线是单 listener，改为实机双 listener 结构）
config/aclfile.example        新增（ACL 角色骨架，已脱敏）
docs/P4-PI-AGENT-SPEC.md      新增（P4 接入契约，密码/IP 已脱敏）
docs/P4-PI-HANDOFF.md         新增（P4 交接与自验，密码/IP 已脱敏）
p4-agent/                     新增（P4 ↔ OpenClaw 桥接适配器，完整目录）
  ├─ app/p4_agent.py          桥接主程序
  ├─ systemd/p4-agent.service
  ├─ .env.example             环境变量样例（值全空）
  └─ README.md
README.md                     重写（底座导向，去掉固件/硬件内容）
systemd/*.service             从实机 /etc/systemd/system/ 同步（原 4 个，后增 p4-bot / car-bot，现 6 个）
.gitignore                    更新（补 password_file / acl_file / *.crt / *.bak.*）

# ---- 2026-10-28 追加 ----
app/p4_bot.py                 新增（P4 设备机器人：企微中文指令 → hub/command/p4）
app/p4_command.py             新增（指令解析表 + ack 人话化）
systemd/pihub-p4-bot.service  新增

# ---- 2026-10-30 追加 ----
app/car_bot.py                新增（曼波小车机器人：企微 → hub/command/car）
app/car_command.py            新增
systemd/pihub-car-bot.service 新增
config/aclfile.example        修改（新增 user car 段）
app/api.py                    修改（HTTP 白名单 + 被动学 C3 的 IP，见 §6 / §7）
.env.example                  修改（三组独立企微凭据 + http_allow）
```

### 本次对 p4_agent.py 的修正（相对原始部署版本）

| 项 | 原值 | 改后 | 理由 |
|---|---|---|---|
| `MQTT_HOST` 默认值 | `<PI_LAN_IP>`（内网 IP） | `127.0.0.1` | 适配器与 broker 同机，走回环即可；去掉硬编码 IP |
| 证书路径默认值 | 绝对路径 `/home/pi/cooperate/p4-agent/certs/*` | 相对本文件解析 → `<repo>/p4-agent/certs/*` | 换部署目录无需改代码 |
| **会话 ID** | `f"conv:p4-{msg_id}"` | `f"conv:{DEVICE_ID}-main"` | **修 bug**：原写法每句一个新会话，多轮上下文全丢（§9.2） |
| **并发闸** | 无限制（每条消息一个线程） | `MAX_CONCURRENT=1` 严格串行，超出回 `agent_busy` | P4 不排队，并发会抢同一气泡（§9.2） |
| **SIGTERM 处理** | 无（只有 `KeyboardInterrupt`） | 注册信号 → `shutdown_evt`；`loop_start()` + `wait()` | **修 bug**：否则 `systemctl stop/restart` 必然超时（§9.2） |
| **帧超限行为** | 打一行 WARN 后静默丢弃 | `_publish_final_error()` 逐级裁短 msg，保证发出 `internal` 终结帧 | **修 bug**：否则 P4 气泡卡到 60s 超时（§9.2） |

> 前两项可用环境变量覆盖，仅影响可移植性。
> **后四项是真 bug 修复，已全部同步回实机并重启验证。**

---

## 11. 复现步骤（从零到跑起来）

> 写给"另一台树莓派"看。

```bash
# 1) 系统依赖
sudo apt update
sudo apt install -y mosquitto mosquitto-clients python3-venv git rsync

# 2) 取代码
git clone <本仓地址> /home/pi/cooperate/pihub
cd /home/pi/cooperate/pihub

# 3) 虚拟环境
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 4) 配置
cp .env.example .env
chmod 600 .env
# 编辑 .env：填 WECOM_CORP_ID / WECOM_BOT_ID / WECOM_BOT_SECRET
#           不接企微可以三个都留空（wecom_bot 会以降级模式运行）

# 5) 建库
.venv/bin/python app/init_db.py
mkdir -p logs

# 6) 录配置（★ 必做，否则发指令必失败）
#    在 config 表里至少设 smalltv_ip；其余阈值用默认值即可
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/pihub.db')
for k, v in [('smalltv_ip', '<你的ESP32局域网IP>'),
             ('pihub_port', '5000'),
             ('mqtt_host', '127.0.0.1'),
             ('mqtt_port', '1883')]:
    c.execute("INSERT INTO config(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
c.commit(); print('config ok')
PY

# 7) Mosquitto —— 仅本机（最简）
#    ★ 用 localonly 那份：mosquitto-pihub.conf 的 8883 段写死了
#      password_file / acl_file / cafile / certfile / keyfile，
#      不先备齐就**直接启动失败**（不是告警）。
sudo cp config/mosquitto-localonly.conf /etc/mosquitto/conf.d/pihub.conf
sudo systemctl restart mosquitto

# 8) Mosquitto —— 接外部设备（需 TLS）
#    生成 CA + 服务端证书（略，见 mosquitto 文档 / deploy 说明）
sudo mkdir -p /etc/mosquitto/certs
#   放 ca.crt / broker.crt / broker.key 到上面目录，chown root:mosquitto
sudo mosquitto_passwd -c /etc/mosquitto/passwd pihub
sudo mosquitto_passwd    /etc/mosquitto/passwd p4
sudo mosquitto_passwd    /etc/mosquitto/passwd agent
sudo cp config/aclfile.example /etc/mosquitto/aclfile
sudo chown root:mosquitto /etc/mosquitto/aclfile /etc/mosquitto/passwd
sudo chmod 640 /etc/mosquitto/aclfile /etc/mosquitto/passwd
sudo systemctl restart mosquitto

# 9) 安装服务（★ 先确认 systemd 单元里的绝对路径与你的部署目录一致）
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pihub-api pihub-devctl pihub-monitor pihub-wecom-bot

# 9b) 可选：P4 Agent 适配器（需要 ESP32-P4 控制面板时才装）
#     它要求 broker 的 8883 TLS 监听已就绪（步骤 8）
cd p4-agent
python3 -m venv .venv && .venv/bin/pip install paho-mqtt
cp .env.example .env && chmod 600 .env
#     填 P4_MQTT_PASS（agent 账号密码）与 P4_GATEWAY_TOKEN
#     证书放到 p4-agent/certs/：ca.crt / agent.crt / agent.key
sudo cp systemd/p4-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now p4-agent
cd ..

# 10) 验证
systemctl is-active pihub-api pihub-devctl pihub-monitor pihub-wecom-bot p4-agent mosquitto
ss -lntp | grep -E '5000|1883|8883'
curl -s localhost:5000/api/health
./acceptance_s9.sh check
```

**本机特有的、不通用之处**（换机器必须改）：

| 项 | 本机值 | 换机器怎么办 |
|---|---|---|
| 部署路径 | `/home/pi/cooperate/pihub` | 改 4 个 systemd 单元里的绝对路径 |
| 运行用户 | `pi` | 改 unit 的 `User=`/`Group=` |
| 证书 | 自签 10 年期，至 2036-10-20 | 重新生成一套（**不要复用本机的**） |
| `smalltv_ip` | 真值在 DB | 填入你自己设备的 IP |
| 企微凭据 | 真值在 `.env` | 建自己的机器人，填自己的 |

---

## 12. 本仓不含哪些东西

> 下表这些**不在仓里**，部署时需要你自己准备（凭据、证书、数据库、密码文件）。
> 仓里只保留**键名与路径**，值一律为空或 `<REDACTED>`。

| 文件 / 字段 | 处理 | 说明 |
|---|---|---|
| `.env` | 整文件不提交 | 只保留键名；3 个企微凭据全为 `<REDACTED>` |
| `data/pihub.db` | 整文件不提交 | 只保留 schema + config 键名；不含 symbols/wecom_chat 数据 |
| `/etc/mosquitto/passwd` | 不提交 | 只写路径 |
| `/etc/mosquitto/aclfile` | 不提交 | 仓内只放 `config/aclfile.example`，用户名为角色名 |
| TLS 私钥 `broker.key` | 不提交 | 只写证书路径 + 有效期 |
| `/etc/mosquitto/certs/*.crt` | 不提交 | `.gitignore` 已挡 `*.crt` |
| 设备 IP（`smalltv_ip`） | 替换 `<REDACTED>` | `.env.example` 中该字段为空 |
| `wecom_chat_id` / `wecom_bot_id` | 替换 `<REDACTED>` | |
| `app/*.bak.2026*`（6 个调试备份） | 不提交 | 含硬编码内网 IP；`.gitignore` 已加 `*.bak.*` |
| `app/__pycache__/` | 不提交 | `.gitignore` 已挡 |
| `p4-agent/.env` | 整文件不提交 | 12 个键，含 `P4_MQTT_PASS` / `P4_GATEWAY_TOKEN`；仓内只放 `.env.example` |
| `p4-agent/certs/*.key`（5 个） | 不提交 | `agent.key` / `p4.key` / `observer.key` / `broker.key` / `ca.key`，`.gitignore` 已挡 `*.key` |
| `p4-agent/certs/*.crt` | 不提交 | 同 `.gitignore` `*.crt` |
| `p4-agent/logs/` | 不提交 | `.gitignore` 已挡 `logs/*` 与 `*.log` |
| `docs/P4-PI-HANDOFF.md` 里的 observer 密码 | **打码** | 原文明文写着一个可用密码，改为 `-P '<REDACTED>'` |
| 内网 IP `<PI_LAN_IP>` | 替换 `<PI_LAN_IP>` | 出现在 HANDOFF 的 Host / 证书 SAN / mosquitto_sub 例句共 3 处 |
| `p4_agent.py` 里的 `<PI_LAN_IP>` 默认值 | **改为 `127.0.0.1`** | 不是打码，是改默认值（走回环更合理） |
| 图片（如有） | 本仓未包含 | 底座不含 assets |

> 上表第三行起还说明了一件事：**能跑起来的最小真值是"你自己生成的东西"** ——
> 一套自己的证书、自己的 `passwd` / `aclfile`、自己的 `.env`。
> 直接照抄别人仓里的示例是跑不起来的（示例里一句真值都没有）。

---

## 13. 自检

- [x] 所有占位符已替换，没有遗留无意义 `<...>`
- [x] 没有任何真实 token / 私钥 / chat id / 设备 MAC / 内网 IP
- [x] `systemd` 单元确认无 `Environment=` 携带密钥
- [x] Mosquitto 的密码/ACL **内容**没有贴（ACL 只贴角色→主题骨架）
- [x] DB 只贴 schema 与 config 键名，没有贴 `symbols` / `wecom_chat` 数据
- [x] 复现步骤是"另一台机器能照做"的粒度，并标出了本机特有项
- [x] 相对基线的差异摘要已写（结论：`app/` 零差异，改动全在仓外）

---

## 14. 一句话

> 这份文件回答的是：**"换一台树莓派，照着它能不能跑起来？"**
> 代码进 git；**这台机器的"仓外状态"进这里**。两者合起来才是可复现的底座。
