# 底座（base）容器化 · 落地手册

> **一句话**：容器边界 = **总线 + 核心**。镜像里只有 `core/`，
> `devices/*`、`api(5000)`、`devctl`、任何企微 bot、`win-agent/` 一律不进。
> **systemd 是第一等运行方式，容器是第二等**（DEPLOY.md §11「照文档装就能跑」必须一直有效）。

---

## 0. 本机环境事实（2026-11-24 23:45 实测，已从 PARTIAL 转 DONE）

| 项 | 实际 |
|---|---|
| 容器引擎 | **Docker 29.1.3**（`pi` 在 `docker` 组） |
| `podman` | **未安装**（所以没有 Quadlet 路线，手册全按 Docker 写） |
| 基础镜像 | 原代理 `<代理地址>:<端口>` **已不可达** → 改用国内镜像源 `docker.m.daocloud.io` 拉取后重打 `python:3.11-slim` 标签 |
| 现状 | `agent-ops-edge-hub-base:latest` **已构建成功**，结构/CLI/容器内实跑三关全过 |

### 代理这件事（踩过的坑，别再走）

`/usr/lib/systemd/system/docker.service.d/http-proxy.conf` 里配了 `<代理地址>:<端口>`，
换热点后该地址已不可达；`NO_PROXY` 只有 `localhost,127.0.0.1`，导致**连国内镜像加速器也被塞进代理**。

改法（已执行，备份 `/tmp/http-proxy.conf.bak.20261124-233509`）：
```ini
[Service]
Environment="NO_PROXY=localhost,127.0.0.1,docker.m.daocloud.io,registry-1.docker.io,auth.docker.io"
```
拉基础镜像：
```bash
docker pull docker.m.daocloud.io/library/python:3.11-slim
docker tag  docker.m.daocloud.io/library/python:3.11-slim python:3.11-slim
docker build -f docker/Dockerfile -t agent-ops-edge-hub-base:latest .
```

---

## 1. 镜像装什么（唯一准入清单）

| 进镜像 | 不进镜像 |
|---|---|
| `core/config.py` `paths.py` `cache.py` `datasource.py` `init_db.py` | `devices/smalltv/*`（含 api/devctl/bot） |
| `core/wecom_base.py` `mqtt_guard.py` `monitor.py` `notify.py` `snapshot.py` | `devices/p4/*` `devices/car/*` |
| `requirements.txt`、`docker/entrypoint.sh` | `win-agent/*`、`ai-agent-wecom/*` |

这个镜像是**无头**的：不含**入口**（人与 Agent 对话的终端），也不含**出口**（消息投递通道）——
入口和出口可以是同一个东西（本机就是企业微信同时担两样），但都不属于底座。

`notify.py` 在镜像里；但它要调的**通知出口命令**（本机是 `openclaw`）默认不在镜像里 ——
「送到哪儿」不属于底座，由使用者提供：留在宿主机，或自行装进镜像。
本机现状是前者：出口在宿主机，`notify` 由 `systemd/pihub-notify.service` 跑；
把出口（命令 + 凭据）装进镜像，`notify` 就能在容器里跑，底座代码一行不改。

**判据**（镜像构建后必须跑，见 §5）：
```bash
docker run --rm agent-ops-edge-hub-base:latest sh -c \
  'ls /opt/pihub; test ! -d /opt/pihub/devices && echo "NO devices/ ✅"'
```
期望输出里**只有** `core/` 和 `entrypoint.sh`。

---

## 2. 构建

```bash
cd /home/pi/cooperate/Agent-Ops-Edge-Hub
# 基础镜像（本机网络需要走国内源，见 §0）
docker tag docker.m.daocloud.io/library/python:3.11-slim python:3.11-slim
docker build -f docker/Dockerfile -t agent-ops-edge-hub-base:latest .
```

### 🔴 `.dockerignore` 必须在 repo 根，且路径要写 `**/`

两个都是实测踩到的：

1. **放 `docker/.dockerignore` 等于没写** —— docker 只认**构建上下文根**下的那个文件，
   而 `Dockerfile` 里是 `COPY core/`，上下文根 = repo 根。
   我第一次放在 `docker/` 里，结果 `core/__pycache__` 被原封不动拷进了镜像。
2. **根级规则不向下递归** —— 这套 BuildKit 上 `__pycache__/` **匹配不到** `core/__pycache__/`，
   必须写 `**/__pycache__`、`**/*.py[cod]`。我用一个 4 行最小工程单独验证过。

验收（必须为空）：
```bash
docker run --rm --entrypoint sh agent-ops-edge-hub-base:latest -c 'find /opt/pihub -name "*.pyc"'
```

---

## 3. 网络：只能 host（三条独立理由）

1. `core/config.py` 的 `mqtt_host` / `pihub_host` 默认 `127.0.0.1`；
   bridge 下容器里的 127.0.0.1 = 容器自己 ⇒ 连不到宿主 mosquitto。
2. **5000 白名单**（api，属 smalltv 插件）在 bridge 下看到的是 docker 网关 IP，白名单全废。
3. **C3 IP 被动学习**（同属插件）在 bridge 下学到的是网关 IP，会把设备 IP 写坏。

⇒ compose 里 `network_mode: host`，没有别的选项。

**mosquitto 留宿主**：本机现役是系统级（ACL/mTLS/passwd 全在 `/etc/mosquitto`），
容器化零收益高成本，**别贸然切**（切了小车和 P4 的 8883 TLS 全断）。
compose 里的 mosquitto 服务挂在 `profiles: ["full"]`，默认不启。

---

## 4. 起 monitor（基线：宿主 mosquitto）

```bash
cd /home/pi/cooperate/Agent-Ops-Edge-Hub
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d monitor
docker compose -f docker/docker-compose.yml logs -f monitor
```

**🔴 先停宿主那个**，否则两路同时往 `hub/alert/#` 发 = 双报：
```bash
sudo -n systemctl stop pihub-monitor
sudo -n systemctl disable pihub-monitor
```

---

## 5. 自检

### 5.0 降级自检（本机现在就能跑，不需要网络）✅ 已实测通过

```bash
cd /home/pi/cooperate/Agent-Ops-Edge-Hub
bash docker/smoke.sh            # 只验结构 + entrypoint + 建库
bash docker/smoke.sh --with-mqtt   # 额外真跑一轮（会发告警，慎用）
```

脚本做四件事，**全部在本机实测通过**：
1. 在 `/tmp` 复刻「镜像里只有 `core/` + `entrypoint.sh`」的布局
2. 断言没有 `devices/`
3. 断言 `core/` **没有任何 `from devices` 反向依赖**（依赖单向铁律）
4. `init_db` 建库 + `entrypoint --help` 正常

### 5.1 镜像成品自检（已实测通过）

```bash
# 结构自检：镜像里没有插件、没有 pyc
docker run --rm --entrypoint sh agent-ops-edge-hub-base:latest -c '
  ls -1 /opt/pihub/core
  test ! -d /opt/pihub/devices  && echo "NO devices/ ✅"
  test ! -d /opt/pihub/win-agent && echo "NO win-agent/ ✅"
  test ! -f /opt/pihub/core/http_api.py && echo "NO api ✅"
  find /opt/pihub -name "*.pyc" | head'

# 代码自检：能 import、CLI 正常
docker run --rm agent-ops-edge-hub-base:latest --help
```

> ⚠️ 想看镜像里有什么时**必须加 `--entrypoint sh`**，否则参数会被转给 monitor：
> `docker run <img> sh -c 'ls'` 会变成 `monitor.py sh -c ls`，报 unrecognized arguments。

### 5.3 容器内实跑（已实测通过）

```bash
mkdir -p /tmp/smokebase/{data,logs}

# ① 首次必须 init_db（空 DB 跑 monitor 会 no such table: symbols）
docker run --rm -u root \
  -v /tmp/smokebase/data:/opt/pihub/data \
  --entrypoint sh agent-ops-edge-hub-base:latest -c 'python3 -m core.init_db'

# ② 跑一轮（需要 --network host 才连得到宿主 127.0.0.1:1883）
docker run --rm --network host -u root \
  -v /tmp/smokebase/data:/opt/pihub/data \
  -v /tmp/smokebase/logs:/opt/pihub/logs \
  agent-ops-edge-hub-base:latest --once --force
```

实测输出：
```
数据库: /opt/pihub/data/pihub.db
已建表 (7): alert_cooldown, config, kline_history, quote_cache, symbols, weather_cache, wecom_chat

2026-11-24 23:41:57 [poll] sh600519 贵州茅台 price=1460.0 pct=1.46% ... fired=-
```

> ⚠️ **`-u root` 是必须的**：容器默认以 root 写 `/opt/pihub/data`，若宿主目录属于 `pi`，
> 用 `-u pi` 会 `attempt to write a readonly database`。要么 `-u root`，要么先 `chown`。
>
> ⚠️ `--once` 命中涨跌停会真发告警。不想扰动用临时 DB（默认无 symbols ⇒ 不会发）。

---

## 6. systemd（第二等：容器也要被 systemd 管）

**Docker 版用 `docker compose` + 一个 oneshot unit**；本机没有 podman，跳过 Quadlet。

> ✅ **本机已落地并用上**：`~/.config/systemd/user/pihub-base-compose.service`，
> 已 `enable --now` 并处于 `active`；`loginctl show-user pi -p Linger` = `yes`。

```ini
# ~/.config/systemd/user/pihub-base-compose.service（本机实际内容）
[Unit]
Description=PiHub base (container, second class) —— monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=%h/cooperate/Agent-Ops-Edge-Hub
Environment=PIHUB_ENV=%h/cooperate/pihub/.env
Environment=PIHUB_DATA=%h/cooperate/pihub/data
Environment=PIHUB_LOGS=%h/cooperate/pihub/logs
ExecStart=/usr/bin/docker compose -f docker/docker-compose.yml up -d monitor
ExecStop=/usr/bin/docker compose -f docker/docker-compose.yml stop monitor

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now pihub-base-compose.service
systemctl --user status pihub-base-compose.service --no-pager
```

**🔴 四条红线：**
1. **只动 `~/.config/systemd/user/`**，不覆盖 `/etc/systemd/system/pihub-*.service`
   （现有 7 个服务继续走系统级，与容器版互斥的是 `pihub-monitor` 一个）。
2. 用户级 unit 未登录不跑；本机 `Linger=yes`，**本机已豁免**，换机要 `loginctl enable-linger <user>`。
3. 容器上已带 `restart: unless-stopped`，**不要再用 `docker compose restart` 绕过 unit**。
4. **三个 `Environment` 不能删**：不设 `PIHUB_ENV` 时 compose 用默认 `../.env`（= repo 根）
   —— 那里没有 `.env` 文件，docker 会把它当目录建出来，容器报
   `IsADirectoryError: /opt/pihub/.env`（**切过去第一次就报了这个，容器无限重启**）。

### 6.1 ⚠️ `user: "0:0"` 是必须的

宿主 `data/` 属主是 `pi`(1000)，而 `python:3.11-slim` 里没有 uid 1000 ⇒ 容器写 DB 会
`attempt to write a readonly database`。compose 的 monitor 段已写死 `user: "0:0"`。
（更干净的做法是自建用户进镜像，或把宿主 `data/` chown 到容器用户 —— 本机取最简的 root。）

---

## 7. 回滚（30 秒）

```bash
systemctl --user disable --now pihub-base-compose.service
docker compose -f docker/docker-compose.yml stop monitor
docker rm -f pihub-monitor
sudo -n systemctl enable --now pihub-monitor
sudo -n systemctl status pihub-monitor --no-pager | head -5
```

**不需要数据迁移**：DB 一直挂在宿主 `data/`，容器只是读者。

> `pihub-monitor.service` 的单元文件一直在 `/etc/systemd/system/` 里，只是被 `disable` 了
> —— 回滚只需 `enable --now`，不用重写单元。

---

## 8. 未做 / 已知缺口（诚实清单）

**已实测（本机 2026-11-24 23:55）**：
- ✅ `docker compose -f docker/docker-compose.yml config` 通过
- ✅ **`docker build` 成功**（走国内源拿基础镜像）
- ✅ 镜像结构：只有 `core/` + `entrypoint.sh`，**无 devices / win-agent / api / `*.pyc`**
- ✅ `bash docker/smoke.sh` 全绿（宿主降级自检）
- ✅ 容器内 `init_db` 建 7 张表；`monitor --once --force` 真取到 `sh600519` 行情
- ✅ **告警端到端**：容器内 publish → `hub/alert/stock/limit_up` → 订阅端（pihub 回环）收到原文 + PUBACK
- ✅ **已切换常驻**：容器 `pihub-monitor` Up；用户级 unit `enabled/active`；
  宿主 `pihub-monitor` `inactive/disabled`；其余 6 个服务全部 `active` 未受影响
- ✅ docker daemon 代理修正（已备份，`NO_PROXY` 已包含国内镜像源）

**未做 / 已知代价**：
- ❌ **podman / Quadlet 路线未实施** —— 本机无 podman，Quadlet 又需 podman ≥4.4。
- ⚠️ **compose 的 mosquitto 段未经本机验证**（`profiles: full`，默认不启；本机 mosquitto 是系统级）。
- ⚠️ **容器起来后日志只进 `docker logs`**，不再 append 到 `pihub/logs/monitor.log`
  （monitor 只在 stdout 是 TTY 时才自己写文件）。看历史：`docker logs --tail 100 pihub-monitor`。

**实测踩到的坑（已写入正文，别重踩）**：
1. `.dockerignore` 必须在 **repo 根**且写 `**/` 形式，否则 `core/__pycache__` 进镜像。
2. `docker run <img> sh -c ...` 会被 entrypoint 吃掉 ⇒ 必须 `--entrypoint sh`，
   且 `--entrypoint` 要放在**镜像名之前**。
3. 首次启动先 `init_db`，否则 `no such table: symbols`。
4. 挂载宿主目录时容器需 `-u root`（否则 readonly database）。
5. **不设 `PIHUB_ENV` 时 docker 会把 `../.env` 当目录建出来** ⇒ 容器报
   `IsADirectoryError: /opt/pihub/.env` 并无限重启。首次切换就栽在这。
6. `publish()` 要等 `is_connected()` 为真（实测约 0.3s）；连上瞬间就发会被判「未受理」
   —— 这是设计上的 fail-fast，不是 bug。

**不阻塞的已知噪音**：
- `monitor` 启动瞬间打一行 `[mqtt] 连接断开 rc=Unspecified error`，但 `is_connected()` 随即 True，
  发布与 PUBACK 正常 ⇒ **噪音不是故障**。现役 `pihub-monitor` 日志里也是成堆出现。
- `core/monitor.py` 以 `python3 -m core.monitor` 调用时依赖 cwd，已固化在 `entrypoint.sh` 的 `cd /opt/pihub`。
