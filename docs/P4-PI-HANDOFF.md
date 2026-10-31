# P4 控制面板 ↔ 树莓派 Agent —— 正式交接文档

> 版本：v1.0 ｜ 生成时间：2026-10-24 01:5x ｜ Pi 侧实现：OpenClaw Agent（派派）
> 本文件是 **Pi 侧已完成、自验全部通过** 的实况交接，供 Windows 侧 CC 编写 `MqttTransport` 使用。
> 契约来源：[`P4-PI-AGENT-SPEC.md`](./P4-PI-AGENT-SPEC.md)（§4/§5 一字未改）。

---

## 一、结论：Pi 侧已就绪，可以开始写 P4 的 MQTT 客户端

SPEC §7 的全部自验项**已通过**（详见第五节）。现在开始写 P4 侧 `MqttTransport`，两侧唯一变量就是 P4 客户端。

---

## 二、连接参数（P4 固件按此填写）

| 项 | 值 | 备注 |
|---|---|---|
| **Host** | `<PI_LAN_IP>` | Pi 当前内网 IP，**会变**（热点网段），页面必须可填 |
| **Port** | `8883` | TLS 专用端口（1883 是旧设备明文回环，P4 不要用） |
| **TLS** | **必须开启，且必须双向认证** | 不提供客户端证书 → broker 直接断连，无任何提示 |
| **TLS 版本** | TLS 1.2 及以上 | broker 配 `tls_version tlsv1.2` |
| **account** | `p4` | |
| **password** | `<REDACTED>` | 真值只在 broker 的 `passwd` 与设备 NVS 里；**本仓不存** |
| **client_id** | `p4-<MAC后6位小写hex>` | 如 `p4-a1b2c3`，需稳定 |
| **keepalive** | 60 s | |
| **clean session** | `true` | |
| **协议版本** | MQTT 3.1.1 | |
| **QoS** | 一律 1 | |

### 证书（三件套，需烧进固件或放 NVS）

| 文件 | 用途 | Pi 上路径 |
|---|---|---|
| `ca.crt` | 校验服务器（自签 CA，10 年有效） | `/home/pi/cooperate/p4-agent/certs/ca.crt` |
| `p4.crt` | P4 客户端证书（双向认证必需） | `/home/pi/cooperate/p4-agent/certs/p4.crt` |
| `p4.key` | P4 私钥 | `/home/pi/cooperate/p4-agent/certs/p4.key` |

- 证书 CommonName：`p4-panel`（仅用于识别，`use_identity_as_username false`，不影响 ACL）
- **证书 SAN 已包含**：`DNS:pihub.local`、`IP:127.0.0.1`、`IP:<PI_LAN_IP>`
- ⚠️ **Pi IP 变了之后证书 SAN 会不匹配**。P4 侧 `esp-mqtt` 需**关闭 hostname 校验**（或用 IP 连接 + 跳过 CN 校验），只靠 CA 链验证。Pi 侧 agent 客户端也是这么做的（`check_hostname=False` + `tls_insecure_set(True)`）。

### 取证书的方式

用户在 Pi 上执行，或直接把这四个文件拷走：

```bash
ls -la /home/pi/cooperate/p4-agent/certs/
# ca.crt  p4.crt  p4.key  （agent.crt/observer.crt 及所有 *.key 不要外传）
```

---

## 三、topic 与 ACL（P4 账号的实际权限）

| 方向 | 主题 | QoS | retain | P4 权限 |
|---|---|---|---|---|
| P4 → Pi | `hub/chat/p4` | 1 | 否 | **write** |
| P4 → Pi | `hub/device/p4/status` | 1 | **是** | **write** |
| Pi → P4 | `hub/chat/p4/reply` | 1 | 否 | **read** |

P4 账号 `p4` **不能**发布到 `hub/chat/p4/reply`（实测被 ACL 拦截），也不能订阅除 reply 外的任何 `hub/*`。
> 如果后续需要 P4 订阅 `hub/command/p4`，请先在契约里定格式再找我改 ACL。

---

## 四、报文（SPEC §5，Pi 侧实现与之一致）

### 4.1 P4 发指令（已验证可被正确处理）

```json
{"v":1,"id":"p4-000123","ts":1730000000,"text":"把客厅的灯关掉"}
```

Pi 侧回执现状（实测第 5、6 节有原始帧）：

```json
{"v": 1, "id": "p4-test-001", "seq": 0, "delta": "能", "final": false}
...
{"v": 1, "id": "p4-test-001", "seq": 16, "delta": "", "final": true}
```

### 4.2 错误回执（四个码，实测已覆盖三个）

```json
{"v": 1, "id": "t-empty", "seq": 0, "final": true, "error": {"code": "bad_request", "msg": "指令内容为空"}}
{"v": 1, "id": "t-v2", "seq": 0, "final": true, "error": {"code": "bad_request", "msg": "协议版本必须是 1"}}
```

| code | Pi 侧触发条件 |
|---|---|
| `bad_request` | `text` 为空/非字符串、`v` 不等于 1 |
| `agent_timeout` | Agent 150 s 硬超时（保活会一直续命，所以 P4 的 60 s 不会被触发） |
| `internal` | Gateway 返回非 2xx、或其他异常 |
| `agent_busy` | **会发**（`p4_agent.py` 的 `MAX_CONCURRENT = 1` 串行闸：上一条还没跑完就再发一条 ⇒ 明确拒绝，**不排队**）。P4 侧按"明确拒绝"处理，不要自动重试 |

### 4.3 保活

Pi 侧每 **20 s** 发一帧 `delta:""`（实测 45 s 长任务期间共发 2 帧空 delta，P4 的 60 s 计时被持续重置）。
⚠️ 注意：`seq` 会因此被打散——**保活帧也占 seq**，正常，P4 只需按 seq 累加 delta。

### 4.4 已知行为，P4 侧需要照单接住

1. **delta 粒度很碎**（实测一帧常是 1~3 个汉字）。这是 Gateway 流式输出的原始粒度，Pi 侧**刻意不合并**——合成大块会牺牲"文字长出来"的观感。P4 只需逐帧追加即可。
2. **文本净化已做**：Pi 侧 `sanitize()` 会剥掉 emoji、`**`、`#`、代码块围栏、行内反引号、列表符号。
   - 但实测发现**反引号剥除有残留**：Agent 输出的 `` `sleep 45` `` 被拆成多帧后，剥除失效，屏幕上可能出现零星反引号。
   - 这是**已知小瑕疵**，P4 侧不用处理；如影响观感我后续在 Pi 侧修（需要做跨帧状态机）。
3. **单帧上限 3500 字节**（契约 4096 留余量），Pi 侧发送前自检，超了按字符切分，不会发出超限帧。

---

## 五、SPEC §7 自验报告（全部通过）

| # | 项 | 结果 |
|---|---|---|
| 7.1 | `ss -tlnp` 显示 `0.0.0.0:8883` + `127.0.0.1:1883` | ✅ |
| 7.1 | TLS 匿名连接被拒 | ✅ `The connection was lost` |
| 7.1 | TLS 有密码但**无客户端证书**被拒 | ✅ `The connection was lost`（双向认证生效） |
| 7.1 | 正确证书+密码连接成功 | ✅ |
| 7.1 | 密码错误被拒 | ✅ `Connection Refused: not authorised` |
| 7.1 | ACL 越权（p4 发 reply）被拦截 | ✅ observer 收不到 |
| 7.2 | 假扮 P4 发指令 | ✅ |
| 7.3 | 60 s 内收到流式多帧、seq 从 0 递升、以 `final:true` 结束 | ✅ 17 帧 |
| 7.3 | `id` 逐字符回传 | ✅ `p4-test-001` |
| 7.3 | 重复 `id` 不二次执行 | ✅ 无任何回复帧，日志 `[dedup] 重复 id=... 忽略` |
| 7.4 | 空 `text` → `bad_request` | ✅ |
| 7.4 | `v=2` → `bad_request` | ✅ |
| 7.4 | 慢任务（45 s）期间有保活、最终正常 final | ✅ 2 帧空 delta + 正常 final |
| 7.4 | 单帧 ≤ 4 KB | ✅ 上限 3500 |
| 7.5 | `hub/#` 全局观察窗口 | ✅ 三主题全部可见 |
| 8 | `hub/chat/*` 非 retained（新订阅者收不到旧消息） | ✅ |
| 8 | `hub/device/p4/status` retained | ✅ 新订阅立刻拿到 |
| 8 | 6 个服务 enabled + active | ✅ |
| 8 | 旧 5 个 pihub 服务不受影响 | ✅ 全 active，回环明文 publish OK |

### 未覆盖项（诚实声明）

- **`agent_timeout` 未实跑**：需要一次真的 150 s 不响应，构造成本高且会污染 Gateway 会话。逻辑路径已 code review。
- **`internal` 未实跑**：需人为让 Gateway 报错。
- **P4 真机未接**（预期如此，固件还没写 MQTT）。

---

## 六、原始收发记录（联调对照用）

一条完整往返（`p4-test-001`）：

```
→ hub/chat/p4 {"v":1,"id":"p4-test-001","ts":1730000000,"text":"用一句话说明你现在能收到 P4 的消息"}

← hub/chat/p4/reply {"v": 1, "id": "p4-test-001", "seq": 0, "delta": "能", "final": false}
← ... seq 1..15 ...
← hub/chat/p4/reply {"v": 1, "id": "p4-test-001", "seq": 16, "delta": "", "final": true}
```

拼起来：`能收到——你这条P4测试消息已经到我这儿，通道正常。`

---

## 七、Pi 侧实现细节（你无需关心，仅供排障参考）

| 项 | 值 |
|---|---|
| Agent 程序 | `/home/pi/cooperate/p4-agent/app/p4_agent.py` |
| systemd | `/etc/systemd/system/p4-agent.service`（enabled + active，Restart=always） |
| 日志 | `/home/pi/cooperate/p4-agent/logs/agent.log` |
| 证书目录 | `/home/pi/cooperate/p4-agent/certs/` |
| broker 配置 | `/etc/mosquitto/conf.d/pihub.conf`（双 listener + per_listener_settings） |
| 密码文件 / ACL | `/etc/mosquitto/passwd`、`/etc/mosquitto/aclfile` |

**架构**：MQTT 收指令 → HTTP 调本机 OpenClaw Gateway `/v1/chat/completions`（`stream:true`）→ 边收边切片发回 MQTT。用的是 `openclaw/main` 模型，与企微话题是**同一套 Agent 能力**（能执行工具、能改文件、能控制设备）。

---

## 八、给 CC 的实现提示（避坑）

1. **先开 TLS 再谈业务**。建议 P4 侧第一步只做：连上 broker + 订阅 `hub/chat/p4/reply` + 收到 retained 的 status。通了再写业务。
2. **`esp-mqtt` 双向认证**：`esp_mqtt_client_config_t.broker.verification.certificate` 放 `ca.crt`；客户端证书用 `broker.verification.client_certificate` / `client_key`（或 `.pem` 合并）。注意 PSRAM 缓冲配置（契约说已配好）。
3. **不要 `retain=true` 发 reply**（Pi 侧也是 false），否则 P4 重启会重放旧回复。
4. **收到 `final:true` 后把该 id 从在途表移除**，之后再收到的同 id 帧直接丢（Pi 侧保证只有一个 final，但 QoS1 可能重复投递）。
5. **保活帧 `delta:""` 必须照常刷新 60 s 计时**，不要因为"空的"就跳过。
6. **`id` 原样回传，别解析**。

---

## 九、等 P4 写完后怎么联调

1. P4 烧好固件 → 打开 Hub app 发一句
2. Pi 侧开观察窗口：
   ```bash
   mosquitto_sub -h <PI_LAN_IP> -p 8883 \
     --cafile /home/pi/cooperate/p4-agent/certs/ca.crt \
     --cert /home/pi/cooperate/p4-agent/certs/observer.crt \
     --key /home/pi/cooperate/p4-agent/certs/observer.key \
     -u observer -P '<REDACTED>' -q 1 -t 'hub/#' -v
   ```
3. 两侧对照看帧序，有问题在这里定位。

---

## 十、安全备注

- 家庭局域网内 **TLS + 双向认证 + 按主题 ACL**，broker **未做端口转发**，不暴露公网。
- `1883` 明文口**只绑 127.0.0.1**，局域网任何机器连不上（旧服务/C3 链路不受影响，也未被削弱）。
- 三套客户端凭据各自独立（`p4` / `agent` / `observer`），任一泄露可单点吊销，不影响其他。
- ⚠️ `p4.key` 是 P4 的私钥，**只在 Windows 侧固件工程内使用**，不要外传或提交到公开仓库。
