# 安全边界

> 本文件回答：**谁被信任、谁能读写什么、哪些口子绝对不许开。**
> 规约层面的「安全」见 [DATA-PLANE-CHARTER.md](DATA-PLANE-CHARTER.md#二--安全唯一被信任的通道是窄而确定的)。

---

## 威胁模型

| 假设 | 后果 |
|---|---|
| 部署在**受信任局域网**（家庭 / 桌面） | 不做公网暴露、不做端口转发 |
| 端侧设备**可能被攻破、被替换、被拆走** | 每台设备独立凭据 + 最窄 ACL，失败范围锁死 |
| 操作者**可能误配**（白名单写 `0.0.0.0/0`、端口转发） | 危险配置**直接拒绝启动**，不靠自觉 |
| **不在范围**：公网攻击、物理接触、供应链、工业安全认证 | 这些需要另一套设计，本仓不声称覆盖 |

---

## 一、MQTT 主线：TLS + 双向证书 + ACL（本项目重心）

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
| 账号隔离 | `password_file`，每设备 / 每角色一个账号 |
| 主题级授权 | `acl_file`，**按主题收窄，绝不给外部角色 `#`**（见下） |
| 明文口隔离 | `1883` 只绑 `127.0.0.1`（`per_listener_settings true`），出不了机器 |

### ACL 粒度：读写分离 + 单向可见

设备侧（P4 面板为例）：

```
user p4                                  # 设备侧
    topic write hub/chat/p4              #   只能发自己的话
    topic write hub/device/p4/status     #   只能报自己的状态
    topic read  hub/chat/p4/reply        #   只能读自己的回复
    topic read  hub/command/p4           #   只能收发给自己的命令
    topic write hub/command/p4/ack       #   只能回自己的回执
```

**六个角色**（全文见 [`config/aclfile.example`](../config/aclfile.example)）：

| 角色 | 权限 | 用途 |
|---|---|---|
| `pihub` | `readwrite hub/alert/#`、`hub/command/#`、`hub/chat/#` | 本机 6 个服务共用，**仅回环** |
| `p4` | 读写自己的 chat / status / command / ack | P4 面板（设备侧） |
| `agent` | 与 `p4` 恰好相反：读 P4 的话、写回复、发命令、读回执 | 跨机器的外部 Agent |
| `win` | 只写 `hub/command/{p4,car}`、只读两者的 `ack` | 外部接入方，**授权最窄**（见下） |
| `car` | `read hub/command/car`、`write hub/command/car/ack` | 小车 |
| `observer` | `read hub/#` | **仅本机排障，不要给外部设备** |

> ★ **`agent` 与 `win` 是同一类角色的两个实例**，差别只在授权范围：
> `agent` 能进 P4 的对话通道（chat / reply / status），`win` 只能发命令、读回执。
> `win` **故意**不给 `hub/chat/*`、`hub/device/*`、`hub/alert/*` 与任何通配 ——
> 它的定位是"只控设备、看不到别的"。参考实现：[`win-agent/`](../win-agent/)。
> 给新接入方开权限时，**照 `win` 的形状开，别照 `agent` 开**。

每台设备**看不到别人的任何主题**。一台设备凭据泄露，影响面锁死在它自己的主题空间内
——**爆炸半径物理隔离**，这也是为什么本项目坚持「每设备独立进程 + 独立证书 + 独立 ACL 角色」。

> ⚠️ **`p4` 的反控两行不加，P4 就收不到命令**：MQTT 3.1.1 下订阅一个被 ACL 拒的主题，
> broker 会拒绝该订阅（mosquitto 默认还会**直接断连**）⇒ 现象是「反复重连、没有明确报错」。
> 排查顺序：**反复重连先查 ACL，别先查网络。**

> ⚠️ **`per_listener_settings true` 时认证 / ACL 必须每个 listener 各写一遍**，不继承全局 ——
> 这是 `config/mosquitto-pihub.conf` 里最容易漏的一处。

---

## 二、HTTP：仅作最小闭环验证，默认只允许回环

| 端口 | 用途 | 监听范围 | 认证 |
|---|---|---|---|
| `5000` | Flask API | `0.0.0.0`（见下） | **IP 白名单**，默认仅 `127.0.0.1` |
| `1883` | MQTT 明文 | `127.0.0.1` | 匿名（仅回环，不出机器） |
| `8883` | MQTT TLS | `0.0.0.0` | 密码 + ACL + 双向证书 |

**HTTP 这条路没有做认证，请如实理解它：**

- 设计意图只是**最小闭环验证**（ESP32 直接 curl 取数据 / 收指令，固件侧零成本）。
- 当前实现是**全局 IP 白名单**（`http_allow`），默认只放行 `127.0.0.1`；
  白名单含 `0.0.0.0/0` 会直接**拒绝启动**，避免手滑放通。
- 白名单项支持 CIDR，但**强烈建议绑设备固定 IP 而非整网段** ——
  网段放通等于同网段所有设备都能读行情、控设备。

> 💡 **要对外用，请改用 MQTT 通道。** 本项目所有「跨设备、跨网络、要长期在线」
> 的场景，都应该走 8883（TLS + 双向证书 + ACL），而不是把 5000 暴露出去。
> HTTP 只是让 ESP32 这类没有 TLS 栈的低成本 MCU 能先跑通。

### 被动学习设备 IP（`smalltv_ip`）

C3 来取数（`/api/stock*`、`/api/weather`）时，Pi 把它的真实源 IP 记进 `smalltv_ip`。
四道边界：只认 **GET**、只认 **5000** 端口、拒绝回环、**只从白名单内学习**；
同一 IP **30 s** 内只写一次库。并且**拒绝把本机地址当成设备** ——
否则验收脚本从 Pi 自己的 wlan0 发一次 GET，就能把控制指令打回本机。

---

## 三、凭据与进程隔离

- **凭据一个都不进仓**：`.env`、`*.key`、`*.crt`、`passwd` 全部 `.gitignore`；
  仓内样例只有键名与 `<REDACTED>`。轮换过密码也一样。
- **systemd 单元里没有密钥**：所有凭据走 `.env`（`p4-agent` 用 `EnvironmentFile=`），
  这样单元文件可以原样进仓。
- **每个机器人一套企微凭据**：`WECOM_*` / `P4_WECOM_*` / `CAR_WECOM_*` 不复用；
  复用会让「这条消息该谁处理」不可预测，且一个泄露连带多台设备。
- **设备侧三套 MQTT 凭据彼此独立**（`p4` / `agent` / `car`），任一泄露可单点吊销。

---

## 四、红线

- **不要把任何端口转发到公网。** 尤其 `5000` —— 白名单之外没有第二道门。
- **白名单默认只含回环**；放开前务必确认你在给谁开门。
- **`8883` 面向的是受信任局域网。** 要跨网访问，请自行加 VPN。
- **`observer` 只给本机排障**，不要分配给任何外部设备。
- **本仓不含任何凭据、证书、私钥、真实 IP、chat id。**
  若你 fork 后填了真实值，**不要推回来**。
- **防火墙不是边界。** 本仓靠「服务自身绑定 + 白名单」，不依赖 ufw；
  若机器会被带到不受信任的网络，**先启用 ufw**（见 [DEPLOY.md §6](../deploy/DEPLOY.md#6-端口与防火墙)）。

---

## 五、路线图：HTTP 加固（尚未实现，欢迎 PR）

HTTP 层缺认证这件事我们不打算长期留着，计划是：

1. **共享密钥头**（`X-Pihub-Key`）—— 最简单，但需固件配合发头
2. **HMAC 签名请求** —— 防重放，适合有时间的设备
3. **mTLS（等价 MQTT 8883）** —— 彻底统一，代价是 MCU 侧 TLS 开销

在那之前，**HTTP 只适合「设备与 Pi 同处受信网段 + 绑固定 IP 白名单」的场景**。